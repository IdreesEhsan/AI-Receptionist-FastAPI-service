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

VAPI_SERVER_SECRET = os.environ["VAPI_SERVER_SECRET"]
BUSINESS_TZ = ZoneInfo("Asia/Karachi")

# Google Calendar setup
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GOOGLE_CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]

def get_calendar_service():
    creds_json = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=creds)


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
        call = message.get("call", {})
        return call.get("id")
    return None


@app.get("/")
async def health_check():
    return {"status": "ok"}


# ---- 1. Check availability (Google Calendar) ----
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
    
    # Auto-correct year if LLM hallucinates
    if date_filter and date_filter.startswith("2024-"):
        date_filter = date_filter.replace("2024", "2026")
        print(f"YEAR CORRECTED: {date_filter}")

    if not date_filter:
        return tool_result(call_id, "Please provide a specific date.")

    try:
        local_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
    except ValueError:
        return tool_result(call_id, "I didn't understand that date.")

    # Define business hours (9 AM to 5 PM in local timezone)
    start_local = datetime.combine(local_date, time(9, 0), tzinfo=BUSINESS_TZ)
    end_local = datetime.combine(local_date, time(17, 0), tzinfo=BUSINESS_TZ)

    # Convert to UTC for Google Calendar API
    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc = end_local.astimezone(ZoneInfo("UTC")).isoformat()

    service = get_calendar_service()
    
    # Query FreeBusy for the day
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
    
    # Generate 1-hour slots and check if they overlap with busy times
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

    # Return both the spoken result and hidden metadata for the LLM
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


# ---- 2. Book appointment (Google Calendar) ----
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

    # Parse the time string
    try:
        start_dt = datetime.fromisoformat(slot_time)
        end_dt = start_dt + timedelta(hours=1)
    except Exception as e:
        print(f"TIME PARSE ERROR: {e}")
        return tool_result(call_id, "I didn't understand the time format. Please try again.")

    service = get_calendar_service()
    
    # Create the event in Google Calendar
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

    try:
        created_event = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()
        print(f"Google Calendar event created: {created_event.get('htmlLink')}")
    except Exception as e:
        print(f"GCAL INSERT ERROR: {e}")
        return tool_result(call_id, "That slot was just taken. Please offer the caller alternative times.")

    # Log to Supabase for history
    try:
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
            "slot_end": end_dt.isoformat()
        }).execute()
    except Exception as e:
        print(f"SUPABASE LOGGING ERROR: {e}")

    return tool_result(call_id, f"Booking confirmed for {full_name}.")


# ---- 3. Escalation logging ----
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