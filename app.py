import os
import json
from datetime import datetime
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    ImageMessageContent,
    TextMessageContent
)

import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ================================================================
# 請改用環境變數，不要把金鑰直接寫死在程式裡
# ================================================================
LINE_CHANNEL_SECRET = "7b925db3520bb968e405fd5a0957b2f0"
LINE_CHANNEL_ACCESS_TOKEN = "ZnuU+N+STgoywyxFR4pA34D/WosolyZJxO580vsFpVNQE2YVdfn+6OI1225IN75wan/7nyKVWWPr5ipYvTzQ92Z7uVStV9SLzTHBPOUgxsquoTsLv+oTDIJHXjQMVu/sZG/2vFC2mT/Xuhlcnv5rdAdB04t89/1O/w1cDnyilFU="
GEMINI_API_KEY = "AIzaSyCbuuOIqiq11X5Wc6TIP-EE6EisYpjUz50"
GOOGLE_SHEET_ID = "1V2iSubJoQ8HmFv9_IxDmG3dnc7pHvQKinHCfHw1Onzo"
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_WORKSHEET_NAME = os.environ.get("GOOGLE_WORKSHEET_NAME", "記帳")

# ================================================================
# 基本檢查
# ================================================================
required_vars = {
    "LINE_CHANNEL_SECRET": LINE_CHANNEL_SECRET,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "GOOGLE_SHEET_ID": GOOGLE_SHEET_ID,
}

missing_vars = [k for k, v in required_vars.items() if not v]
if missing_vars:
    raise ValueError(f"缺少必要環境變數：{', '.join(missing_vars)}")

# ================================================================
# 初始化
# ================================================================
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
gemini_model = genai.GenerativeModel(GEMINI_MODEL)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
    raise FileNotFoundError(
        f"找不到 Google 憑證檔：{GOOGLE_CREDENTIALS_FILE}"
    )

creds = Credentials.from_service_account_file(
    GOOGLE_CREDENTIALS_FILE,
    scopes=SCOPES
)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_WORKSHEET_NAME)


# ================================================================
# 工具函式
# ================================================================
def clean_json_text(text: str) -> str:
    """清理 Gemini 回傳內容，只保留 JSON 文字"""
    text = text.strip()

    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    return text


def ai_read_receipt(image_bytes):
    """把圖片交給 AI 辨識，回傳 dict"""
    prompt = """
請辨識這張收據或發票，並以 JSON 格式回傳以下資訊：
{
  "日期": "YYYY/MM/DD",
  "店家": "店名",
  "金額": 數字（只有數字，不要符號）,
  "分類": "飲食/交通/日用品/水電/醫療/娛樂/其他",
  "備註": "品項簡述"
}

規則：
1. 只回傳 JSON，不要加任何說明文字。
2. 如果日期看不清楚，可用今天日期。
3. 如果金額看不清楚，請盡量推測；若無法推測則填 0。
4. 如果分類不確定，填「其他」。
5. 若圖片模糊，請在備註中說明。
"""

    import io
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    response = gemini_model.generate_content([prompt, image])

    text = clean_json_text(response.text)
    data = json.loads(text)

    # 基本欄位容錯
    if not isinstance(data, dict):
        raise ValueError("AI 回傳格式不是物件 JSON")

    data.setdefault("日期", datetime.now().strftime("%Y/%m/%d"))
    data.setdefault("店家", "未知")
    data.setdefault("金額", 0)
    data.setdefault("分類", "其他")
    data.setdefault("備註", "")

    # 金額轉數字
    try:
        data["金額"] = int(float(str(data["金額"]).replace(",", "").strip()))
    except Exception:
        data["金額"] = 0

    # 日期簡單容錯
    if not str(data["日期"]).strip():
        data["日期"] = datetime.now().strftime("%Y/%m/%d")

    return data


def save_to_sheets(data):
    """把辨識結果寫入 Google Sheets"""
    row = [
        data.get("日期", datetime.now().strftime("%Y/%m/%d")),
        data.get("店家", "未知"),
        data.get("金額", 0),
        data.get("分類", "其他"),
        data.get("備註", "")
    ]
    sheet.append_row(row)
    return row


def download_line_image(message_id):
    """從 LINE 下載圖片內容"""
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        content = blob_api.get_message_content(message_id)

        # 大多數情況是 HTTPResponse，可用 read()
        if hasattr(content, "read"):
            return content.read()

        # 某些情況可能直接是 bytes
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)

        # 再保底處理
        if hasattr(content, "data"):
            return content.data

        raise ValueError("無法讀取 LINE 圖片內容")


def reply_text(reply_token, text):
    """統一文字回覆"""
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )


# ================================================================
# 路由
# ================================================================
@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    if not signature:
        abort(400, "Missing X-Line-Signature")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")
    except Exception as e:
        app.logger.exception("Webhook handle error: %s", e)
        abort(500, f"Webhook error: {str(e)}")

    return "OK", 200


# ================================================================
# LINE 事件處理：圖片
# ================================================================
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    try:
        # 1. 下載圖片
        image_bytes = download_line_image(event.message.id)

        # 2. AI 辨識
        data = ai_read_receipt(image_bytes)

        # 3. 寫入 Google Sheets
        save_to_sheets(data)

        # 4. 回覆結果
        reply = (
            f"✅ 記帳成功！\n\n"
            f"📅 日期：{data.get('日期', '未辨識')}\n"
            f"🏪 店家：{data.get('店家', '未辨識')}\n"
            f"💰 金額：${data.get('金額', 0)}\n"
            f"📂 分類：{data.get('分類', '其他')}\n"
            f"📝 備註：{data.get('備註', '')}\n\n"
            f"已記錄到你的 Google Sheets 🎉"
        )

    except json.JSONDecodeError:
        reply = "⚠️ AI 辨識結果不是有效 JSON，請重試或換一張更清晰的照片。"
    except Exception as e:
        app.logger.exception("handle_image error: %s", e)
        reply = f"❌ 發生錯誤：{str(e)}\n請確認設定、Google 憑證、工作表名稱與圖片內容是否正確。"

    reply_text(event.reply_token, reply)


# ================================================================
# LINE 事件處理：文字
# ================================================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()

    if text == "本月統計":
        try:
            all_data = sheet.get_all_records()
            this_month = datetime.now().strftime("%Y/%m")

            totals = {}
            for row in all_data:
                row_date = str(row.get("日期", ""))
                if row_date.startswith(this_month):
                    category = row.get("分類", "其他")

                    try:
                        amount = int(float(str(row.get("金額", 0)).replace(",", "").strip()))
                    except Exception:
                        amount = 0

                    totals[category] = totals.get(category, 0) + amount

            if totals:
                summary = f"📊 {this_month} 支出統計\n\n"
                for cat, amount in sorted(totals.items(), key=lambda x: -x[1]):
                    summary += f"• {cat}：${amount}\n"
                summary += f"\n💰 合計：${sum(totals.values())}"
            else:
                summary = (
                    f"本月（{this_month}）還沒有記帳記錄喔！\n"
                    f"傳一張收據照片來試試看 📸"
                )

            reply = summary

        except Exception as e:
            app.logger.exception("handle_text summary error: %s", e)
            reply = f"查詢失敗：{str(e)}"
    else:
        reply = (
            "👋 嗨！我是你的 AI 記帳機器人\n\n"
            "📸 傳收據/發票照片 → 自動記帳\n"
            "📊 傳「本月統計」→ 查看本月花費\n\n"
            "試試看拍一張超商發票傳給我！"
        )

    reply_text(event.reply_token, reply)


# ================================================================
# 啟動
# ================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
