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
import time
from fastapi import Request
import threading

# =========================
# ------- CONFIG ----------
# =========================

# Google Sheets: har bosqich uchun alohida Spreadsheet ID
SPREADSHEET_ID_1 = os.getenv("SPREADSHEET_ID_1", "1vNAbd0SHK4Co0aTUU_fzkmGDtijHt69o2cEN0DgbjFk")  # 1-bosqich
SPREADSHEET_ID_2 = os.getenv("SPREADSHEET_ID_2", "1s4Fu_j7S7PW3mkKO3Sboefxoxxb_bQo5WLmifCYH88w")  # 2-bosqich
# Agar worksheet nomi aniq bo‘lsa, kiriting (aks holda birinchi varaq ishlatiladi)
WORKSHEET_TITLE = os.getenv("WORKSHEET_TITLE", "")  # masalan: "Attendance"

# Google Service Account credentials fayli (gspread.service_account uni o‘qiydi)
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

# Telegram bot konfiguratsiyasi
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8388239106:AAF7onMN3FvA8TST-bZO2FKe9yJHon6EtZE")

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
    print("googlesheetsa ulanmadi....--->>>>>>")
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
    if "1" in s or "bakalavr" in s:
        return "1-bosqich"
    if "2" in s or "magistr" in s:
        return "2-bosqich"
    # default holda kelsa ham xatoga yo‘l qo‘ymaymiz:
    return stage_raw

def sheet_for_stage(stage: str, field: str):
    """
    Bosqich va yo'nalishga (field) mos Google Sheet'ni qaytaradi.
    """
    stage_norm = normalize_stage(stage)
    if stage_norm == "1-bosqich":
        ssid = SPREADSHEET_ID_1
    elif stage_norm == "2-bosqich":
        ssid = SPREADSHEET_ID_2
    else:
        raise HTTPException(status_code=400, detail=f"Noma'lum bosqich: {stage}")

    if not ssid:
        raise HTTPException(status_code=500, detail="Spreadsheet ID sozlanmagan.")

    try:
        sh = gs_client.open_by_key(ssid)
        try:
            ws = sh.worksheet(field)   # field nomiga mos worksheet ochamiz
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=field, rows=1000, cols=30)
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

@app.get("/ping")
def ping():
    return {"pong": True}

# --- Keep alive ---
def keep_alive():
    url = os.getenv("RENDER_URL", "https://your-app.onrender.com/ping")
    while True:
        try:
            print("⏳ Pinging self...")
            r = requests.get(url, timeout=10)
            print("✅ Ping status:", r.status_code)
        except Exception as e:
            print("❌ Ping error:", e)
        time.sleep(600)  # 10 daqiqa

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

@app.post("/api/attendances")
def save_attendance(payload: AttendancePayload):
    """
    Davomatni bosqichga mos spreadsheet va fieldga mos worksheet'ga yozadi.
    Format:
    N | F.I.O | Sana | ...
    """
    stage = "1-bosqich" if payload.specialization == "first" else "2-bosqich"
    ws = sheet_for_stage(stage, payload.field)

    date_str = payload.date or datetime.now().strftime("%Y-%m-%d")
    time_str = payload.time or datetime.now().strftime("%H:%M")
    print("Kelgan JSON:", payload.dict())
    # Google Sheetdagi barcha ma’lumotlarni olish
    values = ws.get_all_values()
    if not values:
        values = [["N", "F.I.O"]]  # default header
        ws.update("A1", [values[0]])

    header = values[0]

    # Sana uchun ustun mavjudligini tekshirish
    try:
        date_col_idx = header.index(date_str)
    except ValueError:
        date_col_idx = len(header)
        header.append(date_str)
        ws.update("A1", [header])  # headerni yangilash

    # Student mapping (ism -> qator)
    student_map = {row[1]: i+1 for i, row in enumerate(values[1:], start=1) if len(row) > 1}

    # Qaysi qatorda yozish kerakligini aniqlash
    row_idx = 2  # 1-qator header, 2-dan boshlanadi
    n_counter = len(student_map) + 1

    for student, status in payload.attendance.items():
        if student in student_map:
            row_idx = student_map[student] + 1
            ws.update_cell(row_idx, date_col_idx+1, status)
        else:
            new_row = [""] * len(header)
            new_row[0] = str(n_counter)
            new_row[1] = student
            new_row[date_col_idx] = status
            ws.append_row(new_row, value_input_option="RAW")
            n_counter += 1

    # Statuslar tugaganidan keyin, oxirgi qatorda submit vaqti yoziladi
    last_row = len(ws.get_all_values()) + 1
    ws.update_cell(last_row, date_col_idx+1, f"Submitted at {time_str}")

    return {"ok": True, "stage": stage, "field": payload.field, "date": date_str, "time": time_str}
@app.post("/api/attendance")
def save_attendance(payload: AttendancePayload):
    """
    Davomatni bosqichga mos spreadsheet va fieldga mos worksheet'ga yozadi.
    Jadval formati (misol):
    N | F.I.O | 2025-09-07 |
    1 | Talaba A | present |
    2 | Talaba B | absent  |
    ...
    (oxirida)   | last commit | 09:43:49 AM |
    ---
    Eslatma: bu funksiya talabalarni YANGI ro'yxatga qo'shmaydi — faqat jadvalda mavjud isimlarga yozadi.
    """
    try:
        stage = "1-bosqich" if payload.specialization == "first" else "2-bosqich"
        ws = sheet_for_stage(stage, payload.field)

        date_str = payload.date or datetime.now().strftime("%Y-%m-%d")
        # agar frontend "8:40:39 PM" kabi formatda yuborsa, uni shunday saqlaymiz:
        time_str = payload.time or datetime.now().strftime("%H:%M:%S")
        print("Kelgan JSON:", payload.dict())

        # O'rnatiladigan map — sheetga inson o'qishi qulay holatda yozish uchun:
        DISPLAY_STATUS = {
            "present": "Keldi",
            "absent": "Kelmadi",
            "excused": "Sababli",
        }

        # Jadvalni olib kelamiz (header + satrlar)
        values = ws.get_all_values()
        if not values:
            # Agar jadval butunlay bo'sh bo'lsa — minimal header yaratamiz.
            header = ["N", "F.I.O", date_str]
            ws.update("A1", [header])
            values = ws.get_all_values()
        else:
            header = values[0]
            # Sana ustuni mavjudmi tekshir — yo'q bo'lsa qo'sh
            if date_str not in header:
                header.append(date_str)
                ws.update("A1", [header])
                # header yangilanganini olish uchun qayta o'qiymiz
                values = ws.get_all_values()
                header = values[0]

        # index (0-based) qilib olish
        try:
            date_col_idx = header.index(date_str)  # 0-based index
        except ValueError:
            # bu hol hech bo'lmasligi kerak, lekin himoya uchun:
            date_col_idx = len(header) - 1

        # Talabalar nomi -> qator (1-based row index for gspread)
        student_map = {}
        for row_idx, row in enumerate(values[1:], start=2):  # start=2 chunki sheet qatorlari 1-based va 1-qator header
            if len(row) > 1 and row[1].strip():
                name_norm = row[1].strip().lower()
                student_map[name_norm] = row_idx

        updated = 0
        missing = []

        # Har bir kelgan attendance yoziladi faqat jadvalda bor bo'lsa
        for student, status in payload.attendance.items():
            key = (student or "").strip().lower()
            if not key:
                continue
            row_num = student_map.get(key)
            if row_num:
                display = DISPLAY_STATUS.get(status, status)  # agar mapping bo'lsa O'zbekcha, yo'q bo'lsa original
                # gspread update_cell row, col (1-based). date_col_idx 0-based -> +1
                ws.update_cell(row_num, date_col_idx + 1, display)
                updated += 1
            else:
                # jadvalda topilmadi — skip qilamiz (yoki xohlasangiz append qilsin deb o'zgartirish mumkin)
                missing.append(student)

        # Oxirgi "last commit" qatorini topish yoki qo'shish
        # 2-ustunda "last commit" (yoki oldingi formatlar) bo'lsa yangilaymiz, aks holda qo'shamiz.
        values_after = ws.get_all_values()
        header_after = values_after[0]
        found_last = None
        for rr_idx, rr in enumerate(values_after[1:], start=2):
            if len(rr) > 1 and isinstance(rr[1], str) and rr[1].strip().lower() in (
                "last commit", "last_commit", "last submit", "last_submit", "last", "last_submit"):
                found_last = rr_idx
                break

        if found_last:
            ws.update_cell(found_last, date_col_idx + 1, time_str)
        else:
            # yangi qator yaratamiz: bo'sh N, 2-ustunda "last commit", sana ustunida vaqt
            new_row = [""] * len(header_after)
            # ikkinchi ustun (F.I.O) ga 'last commit' yozamiz
            if len(new_row) >= 2:
                new_row[1] = "last commit"
            else:
                # agar header juda qisqa bo'lsa minimal shakl
                new_row.append("last commit")
            # date ustuniga vaqt qo'yamiz
            if date_col_idx < len(new_row):
                new_row[date_col_idx] = time_str
            else:
                # pad qilish
                while len(new_row) <= date_col_idx:
                    new_row.append("")
                new_row[date_col_idx] = time_str
            ws.append_row(new_row, value_input_option="RAW")

        return {
            "ok": True,
            "stage": stage,
            "field": payload.field,
            "date": date_str,
            "time": time_str,
            "updated": updated,
            "missing": missing,
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    username: str = Form(...),
    fileType: str = Form(...),
    field: str = Form(...),
    specialization: str = Form(...),
):
    try:
        print("✅ Kelgan so‘rov:", specialization, field, fileType, username)
        print("📂 Fayl nomi:", file.filename)

        stage = "1-bosqich" if specialization == "first" or specialization == "bakalavr" else "2-bosqich"

        content = await file.read()
        print("📄 Fayl uzunligi (bytes):", len(content))

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

        chat_id, topic_id = get_chat_and_topic(stage, field)
        print("💬 Chat:", chat_id, "Topic:", topic_id)

        if not chat_id or not topic_id:
            raise HTTPException(status_code=400, detail="Chat yoki Topic ID topilmadi")

        resp = send_doc_to_telegram(
            bot_token=BOT_TOKEN,
            chat_id=chat_id,
            document_bytes=content,
            filename=file.filename,
            caption=caption,
            message_thread_id=topic_id,
        )
        print("📨 Telegram javobi:", resp)

        return {"ok": True, "chat_id": chat_id, "topic_id": topic_id, "resp": resp}

    except Exception as e:
        print("❌ Xato:", str(e))
        raise

# =========================
# ------- RUNNER ----------
# =========================

if __name__ == "__main__":
    # keep-alive threadni ishga tushiramiz
    threading.Thread(target=keep_alive, daemon=True).start()
    # Lokalda bevosita: python3 main.py
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
