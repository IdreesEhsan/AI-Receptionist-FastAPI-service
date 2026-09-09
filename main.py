import os
import json
import hmac
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from google.oauth2 import service_account
from googleapiclient.discovery import build

from db import supabase

app = FastAPI()

# ---- Environment Variables ----
VAPI_SERVER_SECRET = os.environ["VAPI_SERVER_SECRET"]
BUSINESS_TZ = ZoneInfo("Asia/Karachi")

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GOOGLE_CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]

# ---- Helper: Google Calendar Client ----
def get_calendar_service():
    creds_json = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=creds)

# ---- Helper: Vapi Auth ----
def verify_secret(request: Request) -> bool:
    received = request.headers.get("X-Vapi-Secret")
    if not received:
        return False
    return hmac.compare_digest(received, VAPI_SERVER_SECRET)

def log_payload(endpoint: str, body: dict):
    print(f"RAW VAPI PAYLOAD [{endpoint}]:", body)

def get_tool_call(body: dict):
    """Extract tool call ID and arguments from either enveloped or flat payload."""
    message = body.get("message")
    if isinstance(message, dict) and message.get("type") == "tool-calls":
        tool_calls = message.get("toolCalls", [])
        if not tool_calls:
            return None, None
        call = tool_calls[0]
        func = call.get("function", {})
        raw_args = func.get("arguments", call.get("parameters", {}))
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        else:
            args = raw_args if isinstance(raw_args, dict) else {}
        return call.get("id"), args
    if isinstance(body, dict):
        return "flat", body
    return None, None

def tool_result(tool_call_id, result_text: str):
    if tool_call_id == "flat":
        return {"result": result_text}
    return {"results": [{"toolCallId": tool_call_id, "result": result_text}]}

def get_call_id(body: dict):
    message = body.get("message")
    if isinstance(message, dict):
        return message.get("call", {}).get("id")
    return None

# ---- Helper: Core Booking Logic (Reused by book & change) ----
def create_booking_internal(full_name: str, phone: str, email: str, slot_time: str, vapi_call_id: str = None):
    """
    Internal helper to create a booking in Google Calendar and Supabase.
    Returns (event_id, result_message) or raises ValueError.
    """
    # 1. Parse time
    start_dt = datetime.fromisoformat(slot_time)
    end_dt = start_dt + timedelta(hours=1)

    # 2. Reject past dates
    now_local = datetime.now(BUSINESS_TZ)
    if start_dt < now_local:
        raise ValueError("I cannot book appointments for past dates. Please choose a future date.")

    # 3. Check availability (FreeBusy) – Race condition safety
    service = get_calendar_service()
    freebusy_body = {
        "timeMin": start_dt.astimezone(ZoneInfo("UTC")).isoformat(),
        "timeMax": end_dt.astimezone(ZoneInfo("UTC")).isoformat(),
        "items": [{"id": GOOGLE_CALENDAR_ID}]
    }
    freebusy_result = service.freebusy().query(body=freebusy_body).execute()
    busy_times = freebusy_result['calendars'][GOOGLE_CALENDAR_ID].get('busy', [])
    
    if busy_times:
        raise ValueError("That slot was just taken. Please offer the caller alternative times.")

    # 4. Create event in Google Calendar
    event = {
        'summary': f'Consultation: {full_name}',
        'description': f'Phone: {phone}\nEmail: {email}\nCall ID: {vapi_call_id}',
        'start': {
            'dateTime': start_dt.isoformat(),
            'timeZone': 'Asia/Karachi',
        },
        'end': {
            'dateTime': end_dt.isoformat(),
            'timeZone': 'Asia/Karachi',
        },
    }
    created_event = service.events().insert(
        calendarId=GOOGLE_CALENDAR_ID,
        body=event
    ).execute()
    event_id = created_event.get('id')
    print(f"Google Calendar event created: {created_event.get('htmlLink')}")

    # 5. Log to Supabase
    existing = supabase.table("contacts").select("id").eq("phone", phone).execute()
    if existing.data:
        contact_id = existing.data[0]["id"]
    else:
        new_contact = supabase.table("contacts").insert({
            "full_name": full_name,
            "phone": phone,
            "email": email
        }).execute()
        contact_id = new_contact.data[0]["id"]

    supabase.table("bookings").insert({
        "contact_id": contact_id,
        "call_id": vapi_call_id,
        "status": "confirmed",
        "slot_start": start_dt.isoformat(),
        "slot_end": end_dt.isoformat(),
        "google_event_id": event_id
    }).execute()

    return event_id, f"Booking confirmed for {full_name}."

# ---- Health Check ----
@app.get("/")
async def health_check():
    return {"status": "ok"}

# ---- 1. CHECK AVAILABILITY (Google Calendar) ----
@app.post("/check-availability")
async def check_availability(request: Request):
    if not verify_secret(request):
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    body = await request.json()
    log_payload("check-availability", body)
    call_id, args = get_tool_call(body)
    if call_id is None:
        return JSONResponse(status_code=400, content={"message": "Not a tool-calls request"})

    date_filter = args.get("date")
    
    # Auto-correct year if LLM hallucinates 2024
    if date_filter and date_filter.startswith("2024-"):
        date_filter = date_filter.replace("2024", "2026")
        print(f"YEAR CORRECTED: {date_filter}")

    if not date_filter:
        return tool_result(call_id, "Please provide a specific date.")

    try:
        local_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
    except ValueError:
        return tool_result(call_id, "I didn't understand that date.")

    # Business hours: 9 AM to 5 PM in Lahore time
    start_local = datetime.combine(local_date, time(9, 0), tzinfo=BUSINESS_TZ)
    end_local = datetime.combine(local_date, time(17, 0), tzinfo=BUSINESS_TZ)

    # --- FIX: If the entire day is in the past, return no slots ---
    now_local = datetime.now(BUSINESS_TZ)
    if end_local < now_local:
        return tool_result(call_id, "No available slots found for that date. Ask the caller for an alternative date.")

    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc = end_local.astimezone(ZoneInfo("UTC")).isoformat()

    service = get_calendar_service()
    
    freebusy_body = {
        "timeMin": start_utc,
        "timeMax": end_utc,
        "items": [{"id": GOOGLE_CALENDAR_ID}]
    }
    
    try:
        freebusy_result = service.freebusy().query(body=freebusy_body).execute()
    except Exception as e:
        print(f"GOOGLE CALENDAR ERROR: {e}")
        return tool_result(call_id, "I'm having trouble checking availability right now. Please try again later.")
    
    busy_times = freebusy_result['calendars'][GOOGLE_CALENDAR_ID].get('busy', [])
    
    available_slots = []
    current = start_local
    while current < end_local:
        slot_end = current + timedelta(hours=1)
        is_busy = False
        for busy in busy_times:
            busy_start = datetime.fromisoformat(busy['start'].replace('Z', '+00:00'))
            busy_end = datetime.fromisoformat(busy['end'].replace('Z', '+00:00'))
            if not (slot_end <= busy_start or current >= busy_end):
                is_busy = True
                break
        if not is_busy:
            # --- FIX: Only include slots that are in the future ---
            if current >= now_local:
                available_slots.append({
                    "start": current.isoformat(),
                    "end": slot_end.isoformat()
                })
        current = slot_end

    if not available_slots:
        return tool_result(call_id, "No available slots found for that date. Ask the caller for an alternative date.")

    readable = []
    slot_metadata = []
    for slot in available_slots:
        dt = datetime.fromisoformat(slot['start'])
        local_dt = dt.astimezone(BUSINESS_TZ)
        readable.append(local_dt.strftime("%A, %B %d at %-I:%M %p"))
        slot_metadata.append({"start": slot['start'], "end": slot['end']})

    if call_id == "flat":
        return {
            "result": f"Available slots: {'; '.join(readable)}",
            "slot_metadata": slot_metadata
        }
    
    return {
        "results": [{
            "toolCallId": call_id,
            "result": f"Available slots: {'; '.join(readable)}"
        }]
    }

# ---- 2. BOOK APPOINTMENT ----
@app.post("/book-appointment")
async def book_appointment(request: Request):
    if not verify_secret(request):
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    body = await request.json()
    log_payload("book-appointment", body)
    call_id, args = get_tool_call(body)
    if call_id is None:
        return JSONResponse(status_code=400, content={"message": "Not a tool-calls request"})

    vapi_call_id = get_call_id(body)

    full_name = str(args.get("full_name", "")).strip()
    phone = str(args.get("phone", "")).strip()
    email = str(args.get("email", "")).strip()
    slot_time = str(args.get("slot_time", "")).strip()

    if not full_name or not phone or not slot_time:
        return tool_result(call_id, "Missing required booking details. Ask the caller to repeat their name, phone number, and selected time.")

    try:
        event_id, result_msg = create_booking_internal(
            full_name, phone, email, slot_time, vapi_call_id
        )
        return tool_result(call_id, result_msg)
    except ValueError as e:
        return tool_result(call_id, str(e))
    except Exception as e:
        print(f"BOOKING ERROR: {e}")
        return tool_result(call_id, "I had trouble booking that slot. Please try again.")

# ---- 3. LOOKUP APPOINTMENT ----
@app.post("/lookup-appointment")
async def lookup_appointment(request: Request):
    if not verify_secret(request):
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    body = await request.json()
    log_payload("lookup-appointment", body)
    call_id, args = get_tool_call(body)
    if call_id is None:
        return JSONResponse(status_code=400, content={"message": "Not a tool-calls request"})

    phone = str(args.get("phone", "")).strip()
    if not phone:
        return tool_result(call_id, "Please provide the phone number to look up.")

    # Find the contact
    contact = supabase.table("contacts").select("id").eq("phone", phone).execute()
    if not contact.data:
        return tool_result(call_id, "No existing bookings found for that phone number.")

    contact_id = contact.data[0]["id"]
    now_local = datetime.now(BUSINESS_TZ).isoformat()

    # Find the next upcoming booking for this contact
    booking = supabase.table("bookings") \
        .select("*") \
        .eq("contact_id", contact_id) \
        .gte("slot_start", now_local) \
        .order("slot_start", desc=False) \
        .limit(1) \
        .execute()

    if not booking.data:
        return tool_result(call_id, "No upcoming bookings found for that phone number.")

    booking_data = booking.data[0]
    
    # Format the time nicely for the LLM
    start_dt = datetime.fromisoformat(booking_data["slot_start"])
    local_dt = start_dt.astimezone(BUSINESS_TZ)
    readable_time = local_dt.strftime("%A, %B %d at %-I:%M %p")

    return tool_result(
        call_id,
        f"Found booking for {readable_time}. Booking ID: {booking_data['id']}, Event ID: {booking_data.get('google_event_id')}"
    )

# ---- 4. CHANGE APPOINTMENT (Delete old + Create new) ----
@app.post("/change-appointment")
async def change_appointment(request: Request):
    if not verify_secret(request):
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    body = await request.json()
    log_payload("change-appointment", body)
    call_id, args = get_tool_call(body)
    if call_id is None:
        return JSONResponse(status_code=400, content={"message": "Not a tool-calls request"})

    phone = str(args.get("phone", "")).strip()
    new_slot_time = str(args.get("new_slot_time", "")).strip()
    full_name = str(args.get("full_name", "")).strip()
    email = str(args.get("email", "")).strip()

    if not phone or not new_slot_time or not full_name:
        return tool_result(call_id, "Missing required details (phone, new time, and name).")

    # 1. Find the existing booking
    contact = supabase.table("contacts").select("id").eq("phone", phone).execute()
    if not contact.data:
        return tool_result(call_id, "No existing booking found for this phone number.")

    contact_id = contact.data[0]["id"]
    now_local = datetime.now(BUSINESS_TZ).isoformat()
    
    booking = supabase.table("bookings") \
        .select("*") \
        .eq("contact_id", contact_id) \
        .gte("slot_start", now_local) \
        .order("slot_start", desc=False) \
        .limit(1) \
        .execute()

    if not booking.data:
        return tool_result(call_id, "No upcoming booking found to change.")

    booking_data = booking.data[0]
    old_event_id = booking_data.get("google_event_id")
    old_booking_id = booking_data["id"]

    # 2. Delete the old Google Calendar event
    service = get_calendar_service()
    if old_event_id:
        try:
            service.events().delete(
                calendarId=GOOGLE_CALENDAR_ID,
                eventId=old_event_id
            ).execute()
            print(f"Deleted old event: {old_event_id}")
        except Exception as e:
            print(f"ERROR DELETING OLD EVENT: {e}")
            return tool_result(call_id, "I found your booking, but I had trouble deleting the old event. Please try again later.")

    # 3. Delete the old row from Supabase
    try:
        supabase.table("bookings").delete().eq("id", old_booking_id).execute()
        print(f"Deleted old booking row: {old_booking_id}")
    except Exception as e:
        print(f"ERROR DELETING OLD ROW: {e}")
        return tool_result(call_id, "I had trouble updating your booking. Please try again.")

    # 4. Create the new booking (reuse the helper)
    vapi_call_id = get_call_id(body) or f"change-{uuid.uuid4()}"
    try:
        new_event_id, result_msg = create_booking_internal(
            full_name, phone, email, new_slot_time, vapi_call_id
        )
        return tool_result(call_id, f"Appointment changed successfully. {result_msg}")
    except ValueError as e:
        return tool_result(call_id, str(e))
    except Exception as e:
        print(f"CHANGE BOOKING ERROR: {e}")
        return tool_result(call_id, "I had trouble booking the new slot. Please try again.")

# ---- 5. ESCALATION (Unchanged) ----
@app.post("/escalate")
async def escalate(request: Request):
    if not verify_secret(request):
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    body = await request.json()
    log_payload("escalate", body)
    call_id, args = get_tool_call(body)
    if call_id is None:
        return JSONResponse(status_code=400, content={"message": "Not a tool-calls request"})

    vapi_call_id = get_call_id(body)
    if vapi_call_id is None:
        vapi_call_id = f"unknown-{uuid.uuid4()}"
    reason = args.get("reason", "unspecified")

    try:
        supabase.table("call_logs").upsert(
            {"call_id": vapi_call_id, "outcome": "escalated", "escalation_reason": reason},
            on_conflict="call_id"
        ).execute()
    except Exception as e:
        print(f"ESCALATE DB WRITE FAILED for call_id={vapi_call_id}: {e}")
        return tool_result(call_id, "I've noted this needs a callback, though I'm having a technical issue logging it fully right now.")

    return tool_result(call_id, "Escalation logged. Offer the caller a callback.")