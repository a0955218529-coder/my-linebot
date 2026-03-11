# ================================================================
# AI 智慧記帳 LINE Bot - 教學版
# 請依照說明填入你的金鑰
# ================================================================

import os
import json
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, TextMessageContent
)
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

# =============================================
# 步驟一：填入你的金鑰（從各平台複製貼上）
# =============================================

LINE_CHANNEL_SECRET = "7b925db3520bb968e405fd5a0957b2f0"
LINE_CHANNEL_ACCESS_TOKEN = "ZnuU+N+STgoywyxFR4pA34D/WosolyZJxO580vsFpVNQE2YVdfn+6OI1225IN75wan/7nyKVWWPr5ipYvTzQ92Z7uVStV9SLzTHBPOUgxsquoTsLv+oTDIJHXjQMVu/sZG/2vFC2mT/Xuhlcnv5rdAdB04t89/1O/w1cDnyilFU="
GEMINI_API_KEY = "AIzaSyCbuuOIqiq11X5Wc6TIP-EE6EisYpjUz50"
GOOGLE_SHEET_ID = "1V2iSubJoQ8HmFv9_IxDmG3dnc7pHvQKinHCfHw1Onzo"

# =============================================
# 初始化（不需要修改這部分）
# =============================================

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-2.0-flash')

# =============================================
# 步驟二：確認 credentials.json 已放在同一個資料夾
# =============================================

SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).worksheet('記帳')


# =============================================
# 主要功能函式
# =============================================

def ai_read_receipt(image_bytes):
    """把圖片交給 AI 辨識，回傳辨識結果"""
    prompt = """
    請辨識這張收據或發票，並以 JSON 格式回傳以下資訊：
    {
      "日期": "YYYY/MM/DD",
      "店家": "店名",
      "金額": 數字（只有數字，不要符號）,
      "分類": "飲食/交通/日用品/水電/醫療/娛樂/其他",
      "備註": "品項簡述"
    }
    
    如果看不清楚，請在備註欄說明。只回傳 JSON，不要其他文字。
    """
    
    import PIL.Image
    import io
    image = PIL.Image.open(io.BytesIO(image_bytes))
    response = gemini_model.generate_content([prompt, image])
    
    # 清理 AI 回傳的文字，只保留 JSON 部分
    text = response.text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    
    return json.loads(text)


def save_to_sheets(data):
    """把辨識結果寫入 Google Sheets"""
    row = [
        data.get('日期', datetime.now().strftime('%Y/%m/%d')),
        data.get('店家', '未知'),
        data.get('金額', 0),
        data.get('分類', '其他'),
        data.get('備註', '')
    ]
    sheet.append_row(row)
    return row


def download_line_image(message_id):
    """從 LINE 下載圖片"""
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        content = api.get_message_content(message_id)
        return content.read()


# =============================================
# LINE Webhook 接收訊息
# =============================================

@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    """收到圖片時：下載 → AI 辨識 → 寫入 Sheets → 回覆結果"""
    
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        
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
            reply = "⚠️ AI 辨識結果格式有點奇怪，請重試或換一張清晰的照片"
        except Exception as e:
            reply = f"❌ 發生錯誤：{str(e)}\n請確認所有設定是否正確"
        
        api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply)]
        ))


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    """收到文字訊息時的回應"""
    text = event.message.text.strip()
    
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        
        if text == "本月統計":
            try:
                all_data = sheet.get_all_records()
                this_month = datetime.now().strftime('%Y/%m')
                
                totals = {}
                for row in all_data:
                    if str(row.get('日期', '')).startswith(this_month):
                        category = row.get('分類', '其他')
                        amount = int(row.get('金額', 0))
                        totals[category] = totals.get(category, 0) + amount
                
                if totals:
                    summary = f"📊 {this_month} 支出統計\n\n"
                    for cat, amount in sorted(totals.items(), key=lambda x: -x[1]):
                        summary += f"• {cat}：${amount}\n"
                    summary += f"\n💰 合計：${sum(totals.values())}"
                else:
                    summary = f"本月（{this_month}）還沒有記帳記錄喔！\n傳一張收據照片來試試看 📸"
                    
                reply = summary
                
            except Exception as e:
                reply = f"查詢失敗：{str(e)}"
        else:
            reply = (
                "👋 嗨！我是你的 AI 記帳機器人\n\n"
                "📸 傳收據/發票照片 → 自動記帳\n"
                "📊 傳「本月統計」→ 查看本月花費\n\n"
                "試試看拍一張超商發票傳給我！"
            )
        
        api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply)]
        ))


# =============================================
# 啟動伺服器
# =============================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
