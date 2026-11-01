---
name: image-layers-pptx
description: 把一張平面圖（AI 生成的海報、廣告圖、社群圖卡、截圖）拆成可分開編輯的圖層，輸出 PowerPoint (.pptx)：文字變成可直接改字的文字框，主體物件去背成獨立圖片可拖拉縮放，背景自動補洞。全部在本機跑，不用登入任何網站。觸發時機：使用者說「拆圖層」「把圖拆開」「圖片轉可編輯」「圖片轉 PPT」「像 Canva Magic Layers 那樣」「AI 生的圖只想改一個字」「把海報拆成文字跟物件」，或輸入 /image-layers-pptx。不要觸發：要重新生成或畫一張新圖、只要去背一個物件（直接用 rembg 即可）、只要 OCR 抓文字、PDF 轉 PPT、要用 Canva 網站本身操作。
---

# 圖片拆圖層 → PPTX

一張「黏在一起」的圖 → PPTX 一頁：背景圖 + 去背物件 + 可編輯文字框。第 2 頁放原圖對照。

腳本：`scripts/img2layers.py`。以下 `<SKILL>` 代表本 skill 資料夾路徑。

## 0. 環境

```bash
python -c "import rembg, pptx, rapidocr, cv2"
```

缺套件就裝：`python -m pip install rembg python-pptx rapidocr onnxruntime opencv-python-headless pillow`

第一次跑會下載 rembg 去背模型（`isnet-general-use`，約 180 MB，存到 `~/.rembg`），先跟使用者說一聲。

## 1. 拿到圖片

- 要檔案路徑。使用者直接貼在對話裡的圖沒有檔案，請他給路徑或存成檔案。
- 輸出放在圖片旁邊：工作資料夾 `<圖檔名>_layers/`、成品 `<圖檔名>_layers.pptx`。不覆蓋原圖；成品已存在就加 `_2`。

## 2. 拆

```bash
python "<SKILL>/scripts/img2layers.py" analyze -i 圖.png -o 圖_layers
```

產出：
- `layers.json`：文字（內容、位置、顏色、字級、是否直書）與物件清單，下一步要改的就是它
- `preview.png`：原圖上標紅框 `T0..` 文字、綠框 `O1..` 物件
- `layers/background.png`：擦掉文字、挖掉物件、補好洞的背景
- `layers/object_XX.png`：去背物件

常用選項：
- `--grab x,y,w,h`：rembg 沒抓到的物件（小圖示、第二個商品），用原圖像素座標框出來，GrabCut 切出來；可重複
- `--model u2net|isnet-general-use|birefnet-general`：去背模型，主體抓不好時換一個試
- `--min-area 0.004`：物件最小面積比例，碎片太多就調大
- `--no-objects` / `--no-text`：只拆文字 / 只拆物件
- `--keep-bg`：背景不補洞（物件底下保留原圖，物件不打算移動時用，最乾淨）
- `--keep-holes`：預設會把物件輪廓內部填滿（插畫主體跟背景同色時，rembg 只抓得到描邊）；物件真的有鏤空（甜甜圈、杯把）才加這個

## 3. 檢查並修正 layers.json（這步最重要，不能跳）

用 Read 看 `preview.png`、`layers/background.png`，也看原圖，逐項核對：

1. **文字內容**：OCR 模型輸出偏簡體（来/温/级/质），也會漏空白、認錯形近字。照原圖看到的字修正 `text`，原圖是繁體就用繁體。
2. **刪除**：裝飾花紋被當成字、斜排或太小認不準的字 → 直接刪掉那筆。build/render 會依 `erased` 自動把原圖那塊貼回背景，字還在，只是不能編輯。
3. **合併多行**：同一段字被拆成上下兩框（例如「新品」「上市」）→ 留一筆，`text` 用 `\n` 換行（`"新品\n上市"`），`bbox` 改成兩框聯集，`font_px` 取單行字高，再加 `"merged": [被併掉那筆的 id]`。沒加 `merged` 的話，被刪的那筆會被當成誤判貼回原圖，產生疊字。
4. **漏抓**：新增一筆（不用 `id`；`bbox` 用原圖像素，`font_px` ≈ 字高，`color` 用 `#RRGGBB`）。背景上那段字沒被擦，新文字框會疊在原字上，要跟使用者說明。
5. **顏色**：深底淺字（例如深色圓章裡的白字）常估錯，看原圖修 `color`。
6. **字型樣式**（選填欄位，build 會讀）：
   - `as_image: true`：**書法、手寫、印章、特殊設計字**用一般字型會走樣，改成從原圖摳出來的去背圖片（可移動縮放，不能改字）
   - `bold: true`、`underline: true`
   - `font`：中文明體 `PMingLiU`、楷書 `DFKai-SB`、預設黑體 `Microsoft JhengHei`；英文襯線 `Times New Roman` / `Georgia`
   - `align`：`left`（預設）/ `center` / `right`（右對齊的英文標語記得設）
   - 原圖字距拉開的（「農 曆 八 月」）就在 `text` 裡加空白
   - 字級：build/render 會用實際字型量寬度自動貼合框寬；不想自動調就設 `fit: false`，改用 `font_px`
7. **物件**：主要物件都有綠框嗎？少了就回第 2 步加 `--grab` 重跑（重跑會覆蓋 `layers.json`，已改的文字要重改，所以先確定物件再改文字）。
8. **背景**：還看得到字的殘影、或補洞很明顯 → 回報給使用者，通常可接受（被文字框/物件蓋住）。

改完跑模擬對照，左原圖、右重組，看位置、大小、顏色有沒有跑掉：

```bash
python "<SKILL>/scripts/img2layers.py" render -d 圖_layers -o 圖_layers/render.png
```

字明顯偏大偏小就調 `font_px`，位置偏就調 `bbox`，再 render 一次。

## 4. 輸出 PPTX

```bash
python "<SKILL>/scripts/img2layers.py" build -d 圖_layers -o 圖_layers.pptx
```

`--font 名稱` 一次覆蓋全部字型；`--no-original` 不要原圖對照頁。

## 5. 回報

- PPTX 路徑，拆出幾段文字、幾個物件
- 用法：PowerPoint / Google 簡報 / Keynote 打開，點字直接改；物件可拖拉縮放；改完「另存為圖片」或匯出 PNG
- 誠實說明限制（只講這次實際遇到的）：
  - 物件的陰影、倒影留在背景上，物件移太遠會看到原本的陰影
  - 背景補洞是左右內插，照片類背景補的地方會糊，物件移開才會看到
  - 字壓在線條或圖案上時，擦字的地方會有一點殘色
  - 字型是近似；設了 `as_image` 的字保留原字形但不能改字，要改字得自己換字型重打
  - 小圖示、按鈕底框通常留在背景裡，要拆就用 `--grab`
