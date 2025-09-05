# main.py
import os
from datetime import datetime
from typing import Dict, Optional
import json
import uvicorn
import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
import gspread

# =========================
# ------- CONFIG ----------
# =========================

# Google Sheets: har bosqich uchun alohida Spreadsheet ID
SPREADSHEET_ID_1 = os.getenv("SPREADSHEET_ID_1", "")  # 1-bosqich
SPREADSHEET_ID_2 = os.getenv("SPREADSHEET_ID_2", "")  # 2-bosqich
# Agar worksheet nomi aniq bo‘lsa, kiriting (aks holda birinchi varaq ishlatiladi)
WORKSHEET_TITLE = os.getenv("WORKSHEET_TITLE", "")  # masalan: "Attendance"

# Google Service Account credentials fayli (gspread.service_account uni o‘qiydi)
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

# Telegram bot konfiguratsiyasi
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Topic (forum mavzusi) mapping'lari:
#  - Telegram superguruh "Topics" yoqilgan bo‘lishi kerak.
#  - message_thread_id ni bilish uchun /topics botlari yoki API dan foydalanasiz.
#  - Quyidagi mapping'ni yo‘nalishlar bo‘yicha to‘ldiring.
#  - Kerak bo‘lsa bosqichga qarab ham ajrating.
TOPICS_FILE = os.getenv("TOPICS_FILE", "topics.json")

# Telegram topic mapping'larini yuklash
def load_topic_map():
    if not os.path.exists(TOPICS_FILE):
        return {}
    with open(TOPICS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

TOPIC_MAP = load_topic_map()

# Ruxsat etilgan attendance statuslari (frontenddan kelishi mumkin bo‘lgan variantlar)
STATUS_ALIASES = {
    "keldi": "present",
    "kelmadi": "absent",
    "sababli": "excused",
    "present": "present",
    "absent": "absent",
    "excused": "excused",
}

# =========================
# ------ APP SETUP --------
# =========================

app = FastAPI(title="AkademFlow API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # mini-app dev uchun ochiq; prod’da domeningizni kiriting
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Google Sheets client
try:
    gs_client = gspread.service_account(filename=CREDENTIALS_FILE)
except Exception as e:
    raise RuntimeError(f"Google Sheets service account ulanmadi: {e}")

# =========================
# ------ UTILITIES --------
# =========================

def normalize_stage(stage_raw: str) -> str:
    """
    Frontenddan turli ko‘rinishda kelgan bosqich qiymatlarini normallashtiramiz.
    Qabul qilinadigan qiymatlar: '1-bosqich', '2-bosqich', 'Bakalavr', 'Magistr' va h.k.
    """
    s = (stage_raw or "").strip().lower()
    if "1" in s or "bakal" in s:
        return "1-bosqich"
    if "2" in s or "magistr" in s:
        return "2-bosqich"
    # default holda kelsa ham xatoga yo‘l qo‘ymaymiz:
    return stage_raw

def sheet_for_stage(stage: str):
    """
    Bosqichga mos Google Sheet'ni ochib, kerakli worksheet'ni qaytaradi.
    """
    stage_norm = normalize_stage(stage)
    if stage_norm == "1-bosqich":
        ssid = SPREADSHEET_ID_1
    elif stage_norm == "2-bosqich":
        ssid = SPREADSHEET_ID_2
    else:
        # Noma'lum bosqich — xavfsizroq qilish uchun 400 qaytaramiz
        raise HTTPException(status_code=400, detail=f"Noma'lum bosqich: {stage}")

    if not ssid:
        raise HTTPException(status_code=500, detail="Spreadsheet ID sozlanmagan (SPREADSHEET_ID_1 yoki SPREADSHEET_ID_2).")

    try:
        sh = gs_client.open_by_key(ssid)
        if WORKSHEET_TITLE:
            try:
                ws = sh.worksheet(WORKSHEET_TITLE)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title=WORKSHEET_TITLE, rows=1000, cols=20)
        else:
            ws = sh.sheet1
        return ws
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Sheets ulanishida xatolik: {e}")

def slugify(text: str) -> str:
    """
    Hashtag uchun qulay slug (#kurs_ishi, #malumotnoma va h.k.).
    Belgilarni soddalashtirib, bo‘shliqni '_' ga almashtiradi.
    """
    if not text:
        return ""
    t = text.lower()
    for ch in ["’", "‘", "'", "ʻ", "ʼ", "`", "’", "“", "”", ".", ",", ":", ";", "!", "?", "(", ")", "[", "]", "{", "}", "/", "\\", "|", "+", "=", "&", "%", "$", "#", "@", "^", "*", "\""]:
        t = t.replace(ch, " ")
    t = t.replace("—", " ").replace("-", " ")
    t = "_".join([p for p in t.split() if p])
    # max 40-50 belgi yetadi
    return t[:50]

def hashtag_for_filetype(file_type: str) -> str:
    """
    Fayl turidan hashtag yasash.
    maxsus: 'ma'lumotnoma' -> '#malumotnoma'
    """
    if not file_type:
        return ""
    t = file_type.lower().strip()
    # maxsus almashtirishlar
    t = t.replace("ma'lumotnoma", "malumotnoma").replace("maʼlumotnoma", "malumotnoma").replace("ma`lumotnoma", "malumotnoma")
    return f"#{slugify(t)}"

def hashtags(stage: str, field: str, file_type: str) -> str:
    tags = []
    ft = hashtag_for_filetype(file_type)
    if ft:
        tags.append(ft)
    # boshqa narsalarni ham hashtag qilamiz (talabga ko‘ra)
    stage_tag = f"#{slugify(stage)}" if stage else ""
    field_tag = f"#{slugify(field)}" if field else ""
    for tg in (stage_tag, field_tag):
        if tg and tg not in tags:
            tags.append(tg)
    return " ".join(tags)

def get_chat_and_topic(stage: str, field: str):
    """
    topics.json ichidan chat_id va topic_id ni qaytaradi
    """
    stage_norm = normalize_stage(stage)
    stage_data = TOPIC_MAP.get(stage_norm)
    if not stage_data:
        return None, None

    chat_id = stage_data.get("chat_id")
    topic_id = stage_data.get("topics", {}).get(field)
    return chat_id, topic_id


def send_doc_to_telegram(
    bot_token: str,
    chat_id: str,
    document_bytes: bytes,
    filename: str,
    caption: str,
    message_thread_id: Optional[int] = None,
):
    """
    Telegram'ga document yuborish (forum topic bo‘lsa message_thread_id qo‘shiladi)
    """
    if not bot_token or not chat_id:
        raise HTTPException(status_code=500, detail="Telegram BOT_TOKEN yoki chat_id sozlanmagan.")

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    files = {"document": (filename, document_bytes)}
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if message_thread_id:
        data["message_thread_id"] = message_thread_id

    resp = requests.post(url, data=data, files=files, timeout=120)
    try:
        j = resp.json()
    except Exception:
        j = {"ok": False, "error": resp.text}
    if not j.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram xato: {j}")
    return j

# def resolve_topic_id(topic_map: Dict, stage: str, field: str) -> Optional[int]:
#     """
#     stage va field bo‘yicha topic id topadi. Topilmasa None qaytaradi.
#     """
#     stage_norm = normalize_stage(stage)
#     stage_map = topic_map.get(stage_norm) or topic_map.get(stage) or {}
#     return stage_map.get(field)

# =========================
# -------- MODELS ---------
# =========================

# class AttendancePayload(BaseModel):
#     stage: str = Field(..., description="Bosqich: '1-bosqich' yoki '2-bosqich'")
#     field: str = Field(..., description="Yo‘nalish nomi (masalan, 'Iqtisodiyot')")
#     date: Optional[str] = Field(None, description="YYYY-MM-DD format; bo‘lmasa server qo‘yadi")
#     time: Optional[str] = Field(None, description="HH:MM; bo‘lmasa server qo‘yadi")
#     attendance: Dict[str, str] = Field(..., description="{'Talaba F.I.Sh.': 'present/absent/excused' ...}")
#     username: Optional[str] = Field(None, description="Kim jo‘natdi (telegram user)")

#     @validator("attendance")
#     def validate_statuses(cls, v):
#         if not v:
#             raise ValueError("attendance bo‘sh bo‘lishi mumkin emas")
#         # statuslarni normallashtiramiz
#         normalized = {}
#         for student, status in v.items():
#             st = (status or "").strip().lower()
#             st = STATUS_ALIASES.get(st)
#             if st not in ("present", "absent", "excused"):
#                 raise ValueError(f"Noto‘g‘ri status: {status} (talaba: {student})")
#             normalized[student] = st
#         return normalized
class AttendancePayload(BaseModel):
    specialization: str = Field(..., description="'first' yoki 'second'")
    field: str
    date: Optional[str] = None
    time: Optional[str] = None
    attendance: Dict[str, str]
    username: Optional[str] = None

    @validator("attendance")
    def validate_statuses(cls, v):
        if not v:
            raise ValueError("attendance bo‘sh bo‘lishi mumkin emas")
        normalized = {}
        for student, status in v.items():
            st = (status or "").strip().lower()
            st = STATUS_ALIASES.get(st)
            if st not in ("present", "absent", "excused"):
                raise ValueError(f"Noto‘g‘ri status: {status} (talaba: {student})")
            normalized[student] = st
        return normalized

# =========================
# ------- ENDPOINTS -------
# =========================

@app.get("/")
def health():
    return {"ok": True, "service": "AkademFlow API", "version": "1.0"}

# @app.post("/api/attendance")
# def save_attendance(payload: AttendancePayload):
#     """
#     Davomatni bosqichga mos Google Sheet'ga yozadi.
#     Har student uchun bitta qator append qilinadi:
#     Sana, Vaqt, Bosqich, Yo‘nalish, Talaba, Status, Username
#     """
    # date_str = payload.date or datetime.now().strftime("%Y-%m-%d")
    # time_str = payload.time or datetime.now().strftime("%H:%M")
    # username = payload.username or "Anonim"

    # ws = sheet_for_stage(payload.stage)

    # # Birinchi qatorni header sifatida xohlasa — tashqarida yaratib qo‘yish mumkin.
    # # Bu yerda to‘g‘ridan-to‘g‘ri append qilamiz.
    # count = 0
    # for student, status in payload.attendance.items():
    #     ws.append_row(
    #         [date_str, time_str, normalize_stage(payload.stage), payload.field, student, status, username],
    #         value_input_option="RAW"
    #     )
    #     count += 1

    # return {
    #     "ok": True,
    #     "saved": count,
    #     "stage": normalize_stage(payload.stage),
    #     "field": payload.field,
    #     "date": date_str,
    #     "time": time_str,
    # }
@app.post("/api/attendance")
def save_attendance(payload: AttendancePayload):
    """
    Davomatni bosqichga mos Google Sheet'ga yozadi.
    Sana bo‘yicha ustun yaratadi yoki yangilaydi.
    Student allaqachon bo‘lsa — yangilanadi, bo‘lmasa yangi qator qo‘shiladi.
    Oxirgi ustunga submit vaqti yoziladi.
    """
    stage = "1-bosqich" if payload.specialization == "first" else "2-bosqich"
    ws = sheet_for_stage(stage)

    date_str = payload.date or datetime.now().strftime("%Y-%m-%d")
    time_str = payload.time or datetime.now().strftime("%H:%M")
    username = payload.username or "Anonim"

    # Google Sheetdagi barcha ma’lumotlarni olish
    values = ws.get_all_values()
    if not values:
        values = [["N", "F.I.SH"]]  # default header

    header = values[0]

    # Sana uchun ustun bor yoki yo‘qligini tekshirish
    try:
        date_col_idx = header.index(date_str)
    except ValueError:
        date_col_idx = len(header)
        header.append(date_str)
        ws.update("A1", [header])  # headerni yangilash

    # Oxirgi ustun = "So‘nggi submit vaqti"
    if "last_submit" not in [h.lower() for h in header]:
        header.append("Last_Submit")
        ws.update("A1", [header])
    last_submit_idx = header.index("Last_Submit")

    # Student mapping (ism -> qator)
    student_map = {row[1]: i+1 for i, row in enumerate(values[1:], start=1)}

    for student, status in payload.attendance.items():
        if student in student_map:
            row_idx = student_map[student] + 1  # Google Sheets qatori (1-based)
            ws.update_cell(row_idx, date_col_idx+1, status)       # status yozish
            ws.update_cell(row_idx, last_submit_idx+1, time_str)  # oxirgi vaqt
        else:
            new_row = [""] * len(header)
            new_row[1] = student
            new_row[date_col_idx] = status
            new_row[last_submit_idx] = time_str
            ws.append_row(new_row, value_input_option="RAW")

    return {"ok": True, "stage": stage, "field": payload.field, "date": date_str, "time": time_str}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    specialization: str = Form(...),
    field: str = Form(...),
    fileType: str = Form(...),
    username: str = Form("Anonim"),
):
    stage = "1-bosqich" if specialization == "first" else "2-bosqich"

    content = await file.read()
    date_str = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H:%M")

    tags = hashtags(stage, field, fileType)
    caption = (
        f"📁 <b>Yangi fayl yuklandi</b>\n"
        f"👤 <b>Talaba:</b> {username}\n"
        f"🎓 <b>Bosqich:</b> {stage}\n"
        f"🔖 <b>Yo‘nalish:</b> {field}\n"
        f"📝 <b>Fayl turi:</b> {fileType}\n"
        f"📅 <b>Sana:</b> {date_str} {time_str}\n\n"
        f"{tags}"
    )

    # topics.json dan chat_id va topic_id olish
    chat_id, topic_id = get_chat_and_topic(stage, field)
    if not chat_id or not topic_id:
        raise HTTPException(status_code=400, detail="Chat yoki Topic ID topilmadi")

    # Faylni yuborish
    resp = send_doc_to_telegram(
        bot_token=BOT_TOKEN,
        chat_id=chat_id,
        document_bytes=content,
        filename=file.filename,
        caption=caption,
        message_thread_id=topic_id,
    )

    return {"ok": True, "chat_id": chat_id, "topic_id": topic_id, "resp": resp}

# =========================
# ------- RUNNER ----------
# =========================

if __name__ == "__main__":
    # Lokalda bevosita: python3 main.py
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
