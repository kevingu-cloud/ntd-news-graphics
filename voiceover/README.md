# AI 配音工具

把口播稿變成 **AI 配音（MP3）** 和 **字幕（SRT）**。

- 每一行是一段，每段分開生成。改了哪一段，再跑一次就只會重新生成那一段。
- 可以用繁體寫稿：加上 `--t2s`，配音前會自動轉成簡體和大陸用語（軟體→软件、影片→视频），字幕仍然顯示你寫的原文。
- 音量會自動調整到 YouTube 標準（約 -14 LUFS），並做輕度壓縮，讓人聲更集中。

## 安裝（只要做一次）

1. **Python 3.10 以上**
   - Mac：到 python.org 下載安裝
   - Windows：到 python.org 下載安裝，安裝時**勾選「Add python.exe to PATH」**
2. **ffmpeg**
   - Mac：`brew install ffmpeg`
   - Windows：`winget install ffmpeg`
3. **安裝套件**：在專案資料夾裡執行

   ```
   pip install -r voiceover/requirements.txt
   ```

4. **金鑰**（用 Azure 或 ElevenLabs 才需要）：把 `voiceover/.env.example` 複製一份，改名為 `voiceover/.env`，填入金鑰。`.env` 不會上傳到 GitHub。

## 使用

Mac 用 `python3`，Windows 用 `python`。

```
# 免費測試（Edge 聲音，不需要金鑰）
python3 voiceover/voiceover.py voiceover/sample-script.txt

# 換聲音、調語速
python3 voiceover/voiceover.py 稿子.txt --voice zh-CN-YunyangNeural --speed 1.05

# 用繁體寫稿，配音時自動轉成簡體和大陸用語
python3 voiceover/voiceover.py 稿子.txt --t2s

# Azure 官方服務，新聞播報風格
python3 voiceover/voiceover.py 稿子.txt --engine azure --voice zh-CN-YunyangNeural --style newscast

# ElevenLabs（先列出聲音 ID，再指定）
python3 voiceover/voiceover.py --engine elevenlabs --list-voices
python3 voiceover/voiceover.py 稿子.txt --engine elevenlabs --voice 聲音ID
```

成品在 `voiceover/output/<稿子檔名>/`：

| 檔案 | 內容 |
|---|---|
| `voice.mp3` | 完整配音 |
| `voice.srt` | 字幕，可直接匯入剪輯軟體或上傳 YouTube |
| `segments/` | 每一段的音檔（快取用） |

## 頻道的固定聲音

頻道選定的聲音和設定存在 `voiceover/channel-voice.args`（ElevenLabs 的「頻道聲音-自有v1」、v4 模型，加上「放鬆、自信、聲音低一點、節奏快」的語氣提示）。每支影片都用同一行指令：

```
python3 voiceover/voiceover.py 稿子.txt @voiceover/channel-voice.args
```

稿子**一行放一整段**（2～7 句）。AI 一次念一整段，語氣比一句一句分開念連貫。改了哪一段，再跑一次只會重新生成那一段。

某一段想用不同的語氣時，在那一段的開頭自己寫一個標籤，例如 `[Curious, energetic, brisk pace] 那拿什麼墊呢？……`，這一段就會用它取代整體語氣。標籤要短，字幕裡不會出現。

單獨一行寫 `[停頓 8]`，會插入 8 秒的空白、不念稿，執行時會顯示它在成品裡的時間位置。可以用來留位置給原聲片段，或讓章節之間停久一點（例如 `[停頓 0.6]`）。

## 配音引擎

| 引擎 | 說明 |
|---|---|
| `edge`（預設） | 免費、不需要金鑰，聲音和 Azure 相同。是非官方用法，可能突然失效，**只適合測試** |
| `azure` | Azure 官方服務，每月有免費額度，可以用 `--style` 指定說話風格。正式經營頻道建議用這個 |
| `elevenlabs` | 情緒和呼吸感比較自然，字幕時間最準（逐字對時）。免費方案不能商業使用 |

## 常用選項

| 選項 | 說明 |
|---|---|
| `--voice` | 聲音名稱或 ID |
| `--speed` | 語速，`1.0` 為正常，`1.1` 快 10% |
| `--t2s` | 繁體（台灣用語）轉簡體（大陸用語）後再配音 |
| `--style` | Azure 說話風格，例如 `newscast`、`narration-professional` |
| `--stability` | ElevenLabs 穩定度 0～1，低一點情緒較多，太低會不穩 |
| `--language` | ElevenLabs：指定語言，例如 `zh`。可以避免 AI 把某些中文詞念成像英文的音 |
| `--direction` | ElevenLabs v3、v4：用幾個英文詞描述整支配音的語氣，會自動加在每一段前面，字幕不受影響。**要短**：實測超過一百個字元的提示，會讓 AI 常常多念一句 |
| `--gap` | 段落之間停頓幾秒，預設 `0.4` |
| `--filter` | 額外的 ffmpeg 音訊濾鏡，例如調音色的等化器，在音量標準化之前套用 |
| `--no-compress` | 不做輕度壓縮，保留原本的音量起伏 |
| `@檔名` | 從檔案讀取參數（每行一個），例如 `@voiceover/channel-voice.args` |
| `--max-chars` | 每句字幕最多幾個字，預設 `18` |
| `--list-voices` | 列出可用的聲音 |

## 讓聲音更有感情（ElevenLabs v3）

用 `--model eleven_v3`，並在稿子裡加情緒標籤，例如：

```
[excited] 好消息是，营收比市场预期的还要好！
[sighs] 结果呢，股价一度跌了百分之七。
[curious] 那这事儿，到底是短期阵痛，还是危险信号？
```

標籤只會送給 v3，字幕裡會自動拿掉；換成其他引擎或模型時，標籤也會自動略過，不會被念出來。範例見 `sample-script-v3.txt`。

- `--stability 0`：創意模式，情緒最多，偶爾會不穩
- `--stability 0.5`：自然模式（預設）
- 用 v2 模型時，可以用 `--stability 0.3 --style-strength 0.45` 加強表現力

## 自動檢查

用 ElevenLabs 配音時，AI 偶爾會多念一句稿子上沒有的話（例如把上一句重複一遍）。工具在每一段念完後，會從成品音檔量出每個字的時間；如果開頭、中間或結尾有超過 1.2 秒對不上稿子，就自動重念，最多 3 次，仍然不行會提示你人工聽那一段。字幕的時間也是用這個量測結果，所以加了語氣提示也對得準。

## 修正讀錯的字

在 `voiceover/lexicon.txt` 加一行「原文=實際要念的寫法」，例如：

```
2026年=二零二六年
GDP=G D P
```

配音時會自動替換，字幕仍然顯示原文。

## 稿子寫法

見 [`docs/ai-voiceover-notes.md`](../docs/ai-voiceover-notes.md) 的「口播稿寫法」。
