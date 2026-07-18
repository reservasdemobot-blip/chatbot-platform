import requests
import time
import json
from datetime import datetime, timedelta, timezone
import pytz
import threading
import os
import sys
import traceback

from googleapiclient.discovery import build
from google.oauth2 import service_account
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================

CONFIG_CACHE = None
CONFIG_LAST_LOAD = 0
CONFIG_TTL = 10  # segundos (ajustable)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if len(sys.argv) < 2:
    print("Uso:")
    print("py bot.py <cliente>")
    sys.exit(1)

CLIENT_NAME = sys.argv[1]

CLIENT_FOLDER = os.path.join(
    BASE_DIR,
    "..",
    "clientes",
    CLIENT_NAME
)

load_dotenv(os.path.join(CLIENT_FOLDER, ".env"))

CONFIG_FILE = os.path.join(CLIENT_FOLDER, "config.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

TZ = pytz.timezone("Europe/Madrid")

offset = 0

user_data = {}

sent_reminders = set()

# =========================
# GOOGLE CALENDAR SETUP
# =========================

SCOPES = ["https://www.googleapis.com/auth/calendar"]

credentials = service_account.Credentials.from_service_account_file(
    os.path.join(CLIENT_FOLDER, "credentials.json"),
    scopes=SCOPES
)

calendar_service = build("calendar", "v3", credentials=credentials)

def get_config():
    global CONFIG_CACHE, CONFIG_LAST_LOAD

    now = time.time()

    if CONFIG_CACHE is None or (now - CONFIG_LAST_LOAD) > CONFIG_TTL:

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            CONFIG_CACHE = json.load(f)

        CONFIG_LAST_LOAD = now

        print("🔄 Config recargada")

    return CONFIG_CACHE

def cfg():
    return get_config()

def get_therapists():
    return get_config()["therapists"]

def has_multiple_therapists():
    return len(get_therapists()) > 1


# =========================
# HORARIOS BASE
# =========================


def get_next_days():

    days = []

    for i in range(1, 8):

        d = datetime.now() + timedelta(days=i)

        date_str = d.strftime("%Y-%m-%d")

        days.append({
            "date": date_str,
            "label": format_day_label(date_str)
        })

    return days

# =========================
# CALENDAR LOGIC
# =========================

def build_day_range(day):

    date = datetime.strptime(day, "%Y-%m-%d")

    start = datetime(date.year, date.month, date.day, 0, 0)
    end = datetime(date.year, date.month, date.day, 23, 59)

    return (
        start.isoformat(timespec="seconds") + "Z",
        end.isoformat(timespec="seconds") + "Z"
    )
    
def format_day_label(date_str):
    dias = {
        "Monday": "Lunes",
        "Tuesday": "Martes",
        "Wednesday": "Miércoles",
        "Thursday": "Jueves",
        "Friday": "Viernes",
        "Saturday": "Sábado",
        "Sunday": "Domingo"
    }

    d = datetime.strptime(date_str, "%Y-%m-%d")

    return f"{dias[d.strftime('%A')]} {d.strftime('%d/%m')}"

def format_date(day):

    dias = {
        "Monday": "Lunes",
        "Tuesday": "Martes",
        "Wednesday": "Miércoles",
        "Thursday": "Jueves",
        "Friday": "Viernes",
        "Saturday": "Sábado",
        "Sunday": "Domingo"
    }

    d = datetime.strptime(day, "%Y-%m-%d")

    return f"{dias[d.strftime('%A')]} {d.strftime('%d/%m/%Y')}"

def get_events_for_day(day, therapist_id):

    start, end = build_day_range(day)

    calendar_id = get_therapists()[therapist_id]["calendar"]

    events = calendar_service.events().list(
        calendarId=calendar_id,
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    return events.get("items", [])


def get_available_hours(day, therapist_id):

    events = get_events_for_day(day, therapist_id)

    occupied = []

    for e in events:
        if "dateTime" in e["start"]:
            hour = e["start"]["dateTime"][11:16]
            occupied.append(hour)

    base = get_therapists()[therapist_id]["hours"]

    return [h for h in base if h not in occupied]

def is_slot_free(therapist_id, day, hour):
    return hour in get_available_hours(day, therapist_id)

def create_event(
    service_name,
    day,
    hour,
    therapist_id,
    chat_id,
    name=None,
    phone=None
):

    # 🔴 CHECK FINAL (CRÍTICO)
    if not is_slot_free(therapist_id, day, hour):
        raise Exception("SLOT_ALREADY_TAKEN")

    date = datetime.strptime(day, "%Y-%m-%d")

    hour_int = int(hour.split(":")[0])

    start_naive = datetime(
        date.year,
        date.month,
        date.day,
        hour_int,
        0
    )

    start = TZ.localize(start_naive)
    end = start + timedelta(minutes=30)
    
    calendar_id = get_therapists()[therapist_id]["calendar"]
    
    event = {
        "summary": f"{service_name} - {name}",
        "description": (
            f"📱 Tel: {phone}\n"
            f"👤 Cliente: {name}\n"
            f"🆔 Telegram: {chat_id}"
        ),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Europe/Madrid"
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Europe/Madrid"
        }
    }

    calendar_service.events().insert(
        calendarId=calendar_id,
        body=event
    ).execute()


def get_user_events(chat_id):
    events_found = []

    for therapist_id, therapist in get_therapists().items():

        events = calendar_service.events().list(
            calendarId=therapist["calendar"],
            singleEvents=True,
            orderBy="startTime"
        ).execute().get("items", [])

        for e in events:

            desc = e.get("description", "")

            if f"🆔 Telegram: {chat_id}" in desc:

                events_found.append({
                    "id": e["id"],
                    "summary": e.get("summary"),
                    "start": e["start"]["dateTime"],
                    "therapist": therapist_id
                })

    return events_found

def check_reminders():
    while True:
        try:
            now = datetime.now(TZ)
            limit = now + timedelta(hours=24)

            for therapist_id, therapist in get_therapists().items():

                events = calendar_service.events().list(
                    calendarId=therapist["calendar"],
                    singleEvents=True,
                    orderBy="startTime"
                ).execute().get("items", [])

                for e in events:

                    if "dateTime" not in e["start"]:
                        continue

                    start_str = e["start"]["dateTime"]
                    start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))

                    if now <= start_dt <= limit:

                        key = e["id"]

                        if key in sent_reminders:
                            continue

                        desc = e.get("description", "")

                        # extraer chat_id
                        if "🆔 Telegram:" in desc:
                            chat_id = desc.split("🆔 Telegram: ")[1].strip()

                            send(
                                chat_id,
                                cfg()["messages"]["reminder"]
                            )

                            sent_reminders.add(key)

        except Exception as e:
            print("Error en recordatorios:", e)

        time.sleep(60 * 30)  # cada 30 minutos

def delete_event(therapist_id, event_id):
    calendar_service.events().delete(
        calendarId=get_therapists()[therapist_id]["calendar"],
        eventId=event_id
    ).execute()
    
def safe_get_user(chat_id):
    if chat_id not in user_data:
        user_data[chat_id] = {}
    return user_data[chat_id]

# =========================
# TELEGRAM
# =========================

def send_main_menu(chat_id):

    keyboard = {
        "inline_keyboard": []
    }

    for button in cfg()["main_menu"]:

        keyboard["inline_keyboard"].append([{
            "text": button["text"],
            "callback_data": button["action"]
        }])

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": cfg()["messages"]["welcome"],
            "reply_markup": keyboard
        }
    )


def send_admin_menu(chat_id):
        
        keyboard = {
            "inline_keyboard": [
                [{"text": cfg()["messages"]["admin_menu_upcoming_dates"], "callback_data": "admin_next"}],
                [{"text": cfg()["messages"]["admin_menu_all_dates"], "callback_data": "admin_all"}],
                [{"text": cfg()["messages"]["admin_menu_view_professionals"], "callback_data": "admin_therapists"}],
            ]
        }

        send(chat_id, cfg()["messages"]["dashboard_admin"], keyboard)

def get_updates():
    global offset

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    r = requests.get(
        url,
        params={
            "offset": offset,
            "timeout": 20
        },
        timeout=25
    )

    return r.json().get("result", [])


def send(chat_id, text, keyboard=None):

    data = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        data["reply_markup"] = keyboard

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json=data
    )

def answer_callback(callback_id):

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        json={
            "callback_query_id": callback_id
        }
    )

# =========================
# LOOP
# =========================

print("Bot funcionando...")

threading.Thread(target=check_reminders, daemon=True).start()

print("Limpiando cola de Telegram...")

r = requests.get(
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
)

updates = r.json().get("result", [])

if updates:
    offset = updates[-1]["update_id"] + 1

while True:
    
    try:

        updates = get_updates()

        for u in updates:

            offset = u["update_id"] + 1

            # =========================
            # BOTONES
            # =========================
            if "callback_query" in u:

                callback_id = u["callback_query"]["id"]

                answer_callback(callback_id)

                chat_id = str(u["callback_query"]["message"]["chat"]["id"])
                data = u["callback_query"]["data"]

                print("BOTON:", data)

                d = safe_get_user(chat_id)
                print("USER DATA:", user_data)

                # -------------------------
                # MENU
                # -------------------------
                
                if data.startswith("admin_"):

                    if int(chat_id) != ADMIN_ID:
                        send(chat_id, cfg()["messages"]["admin_no_autorized"])
                        continue

                    if data == "admin_next":
                        send(chat_id, cfg()["messages"]["admin_next_dates"])
                        continue

                    if data == "admin_all":
                        send(chat_id, cfg()["messages"]["admin_all_dates"])
                        continue

                    if data == "admin_therapists":
                        text = cfg()["messages"]["admin_professionals"]

                        for t_id, t in cfg()["therapists"].items():
                            text += f"- {t['name']}\n"

                        send(chat_id, text)
                
                if data == "reserve":

                    therapists = get_therapists()

                    if len(therapists) == 1:

                        therapist_id = list(therapists.keys())[0]

                        d = safe_get_user(chat_id)
                        d.clear()
                        d["therapist"] = therapist_id

                        data = f"therapist_{therapist_id}"

                    else:

                        keyboard = {
                            "inline_keyboard": []
                        }

                        for therapist_id, therapist in therapists.items():

                            keyboard["inline_keyboard"].append([{
                                "text": therapist["name"],
                                "callback_data": f"therapist_{therapist_id}"
                            }])

                        send(chat_id, cfg()["messages"]["ask_professional"], keyboard)

                        continue
                    
                elif data == "cancel":

                    events = get_user_events(chat_id)

                    if not events:
                        send(chat_id, cfg()["messages"]["no_events"])
                        continue

                    keyboard = {
                        "inline_keyboard": []
                    }

                    for e in events:

                        dt = e["start"][11:16]

                        keyboard["inline_keyboard"].append([{
                            "text": f"❌ {e['summary']} - {dt}",
                            "callback_data": f"del_{e['therapist']}_{e['id']}"
                        }])

                    send(chat_id, cfg()["messages"]["choose_reservation_cancel"], keyboard)
                
                elif data.startswith("del_"):

                    _, therapist_id, event_id = data.split("_", 2)

                    delete_event(therapist_id, event_id)

                    send(chat_id, cfg()["messages"]["cancel_ok"])

                elif data == "prices":

                    text = cfg()["messages"]["prices_title"]

                    for service_cfg in cfg()["services"].values():
                        text += (
                            f"{service_cfg['icon']} "
                            f"{service_cfg['name']} : "
                            f"{service_cfg['price']}€\n"
                        )

                    send(chat_id, text)

                elif data == "hours":

                    h = cfg()["hours"]

                    send(
                        chat_id,
                        cfg()["messages"]["hours"].format(
                            weekdays=h["weekdays"],
                            saturday=h["saturday"]
                        )
                    )
                
                elif data == "location":
                    
                    location = cfg()["location"]

                    send(
                        chat_id,
                        cfg()["messages"]["location"].format(
                            address=location["address"],
                            maps=location["maps"]
                        )
                    )

                # -------------------------
                # SERVICIO
                # -------------------------
                elif data.startswith("therapist_"):

                    therapist_id = data.replace("therapist_", "")
                    
                    d = safe_get_user(chat_id)
                    d.clear()
                    d["therapist"] = therapist_id

                    keyboard = {
                        "inline_keyboard": []
                    }

                    for service_id, service_cfg in cfg()["services"].items():

                        keyboard["inline_keyboard"].append([{
                            "text": f"{service_cfg['icon']} {service_cfg['name']}",
                            "callback_data": service_id
                        }])
                    
                    send(
                        chat_id,
                        cfg()["messages"]["selected_professional"].format(
                            therapist=get_therapists()[therapist_id]["name"],
                            choose_service=cfg()["messages"]["choose_service"]
                        ),
                        keyboard
                    )
                
                elif data in cfg()["services"]:

                    d["service"] = cfg()["services"][data]["name"]

                    d.pop("day", None)
                    d.pop("hour", None)


                    days = get_next_days()

                    available_days = []

                    for day in days:
                        if get_available_hours(day["date"], d["therapist"]):
                            available_days.append(day)

                    if not available_days:
                        send(chat_id, cfg()["messages"]["no_date_available"])
                        continue

                    keyboard = {
                        "inline_keyboard": [
                            [{
                                "text": f"📅 {day['label']}",
                                "callback_data": f"day_{day['date']}"
                            }]
                            for day in available_days
                        ]
                    }

                    send(chat_id, cfg()["messages"]["choose_day"], keyboard)
                    
                # -------------------------
                # DÍA
                # -------------------------
                elif data.startswith("day_"):

                    selected_date = data.replace("day_", "")
                    
                    d["day"] = selected_date
                    d.pop("hour", None)

                    therapist_id = d.get("therapist")

                    if not therapist_id:
                        send(chat_id, cfg()["messages"]["session_restarted"])
                        continue

                    available = get_available_hours(selected_date, therapist_id)

                    keyboard = {
                        "inline_keyboard": [
                            [{"text": f"🕒 {h}", "callback_data": f"hour_{h}"}]
                            for h in available
                        ]
                    }

                    send(chat_id, cfg()["messages"]["choose_hour"], keyboard)

                # -------------------------
                # HORA FINAL
                # -------------------------
                elif data.startswith("hour_"):

                    chat_id = str(chat_id)

                    d = user_data.get(chat_id)

                    # 🔴 protección 1: sesión inexistente
                    if not d:
                        send(chat_id, cfg()["messages"]["session_expired"])
                        continue

                    # 🔴 protección 2: estado incompleto (MUY IMPORTANTE)
                    if "therapist" not in d or "day" not in d or "service" not in d:
                        send(chat_id, cfg()["messages"]["session_incomplete"])
                        continue

                    hour = data.replace("hour_", "")
                    d["hour"] = hour

                    send(chat_id, cfg()["messages"]["ask_name"])

            # =========================
            # MENSAJES
            # =========================
            
            if "message" in u:

                chat_id = str(u["message"]["chat"]["id"])
                text = u["message"].get("text", "").strip()

                # =========================
                # COMANDOS
                # =========================
                
                if text.startswith("/"):

                    if text in ["/start", "/iniciar"]:

                        d = safe_get_user(chat_id)
                        d.clear()
                        send_main_menu(chat_id)
                        continue

                    elif text == "/admin":

                        if int(chat_id) != ADMIN_ID:
                            send(chat_id, cfg()["messages"]["admin_no_pass"])
                            continue

                        send_admin_menu(chat_id)
                        continue

                    else:
                        send(chat_id, cfg()["messages"]["unknown_command"])
                        continue
                
                
                # =========================
                # MENSAJES FUERA DE UNA RESERVA
                # =========================
                
                if chat_id not in user_data or not user_data[chat_id]:

                    send(chat_id, cfg()["messages"]["unknown_message"])
                    continue
                
                # Solo si hay sesión activa
                
                if chat_id in user_data:

                    d = safe_get_user(chat_id)
                    
                    # =========================
                    # Si todavía no ha elegido una hora, no aceptar mensajes
                    # =========================
                    if "hour" not in d:
                        send(chat_id, cfg()["messages"]["choose_hour_first"])
                        continue

                    # =========================
                    # NOMBRE
                    # =========================
                    if "name" not in d:

                        name = text.strip()

                        # validar que no haya números
                        if any(char.isdigit() for char in name) or len(name) < 3:
                            send(chat_id, cfg()["messages"]["invalid_name"])
                            continue

                        d["name"] = name
                        send(chat_id, cfg()["messages"]["ask_phone"])
                        continue

                    # =========================
                    # TELÉFONO
                    # =========================
                    if "phone" not in d:

                        phone = text.strip()

                        cleaned = phone.replace("+", "").replace(" ", "")

                        if not cleaned.isdigit():
                            send(chat_id, cfg()["messages"]["invalid_phone"])
                            continue

                        # =========================
                        # NORMALIZAR ESPAÑA
                        # =========================
                        if cleaned.startswith("34"):
                            cleaned = cleaned[2:]

                        # Validación final (España)
                        if len(cleaned) != 9:
                            send(chat_id, cfg()["messages"]["invalid_phone"])
                            continue

                        d["phone"] = cleaned

                        # =========================
                        # DOBLE RESERVA
                        # =========================
                        
                        available = get_available_hours(d["day"], d["therapist"])
                        
                        hour = d.get("hour")

                        if not hour:
                            send(chat_id, cfg()["messages"]["session_incomplete"])
                            user_data.pop(chat_id, None)
                            continue

                        if hour not in available:
                            send(chat_id, cfg()["messages"]["reservation_fail"])
                            user_data.pop(chat_id, None)
                            continue

                        print("CREANDO EVENTO...")
                        
                        

                        try:
                            create_event(
                                d["service"],
                                d["day"],
                                hour,
                                d["therapist"],
                                chat_id,
                                d.get("name"),
                                d.get("phone")
                            )
                            
                            print("EVENTO CREADO")

                        except Exception as e:

                            if str(e) == "SLOT_ALREADY_TAKEN":
                                send(chat_id, cfg()["messages"]["slot_taken"])
                                user_data.pop(chat_id, None)
                                continue

                            print("ERROR CALENDAR:", e)
                            send(chat_id, cfg()["messages"]["calendar_error"])
                            continue

                        msg = cfg()["messages"]["reservation_ok"].format(
                            therapist=get_therapists()[d["therapist"]]["name"],
                            service=d["service"],
                            date=format_date(d["day"]),
                            hour=hour,
                            name=d["name"],
                            phone=d["phone"]
                        )

                        send(chat_id, msg)

                        user_data.pop(chat_id, None)
                        continue
                    
    except KeyboardInterrupt:
        raise
    
    except Exception:
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)

    time.sleep(0.1)