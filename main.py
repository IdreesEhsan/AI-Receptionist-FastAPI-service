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
        return message.get("call", {}).get("id")
    return None


def create_booking_internal(full_name: str, phone: str, email: str, slot_time: str, vapi_call_id: str = None):
    """Shared helper: create a booking in Google Calendar + Supabase, or raise ValueError.

    ROLLBACK FIX (found via live Postman testing): the Calendar event insert
    and the Supabase writes are two separate external calls with no shared
    transaction between them. A real test call hit a transient
    ConnectionTerminated error on the Supabase side AFTER the Calendar event
    had already been created successfully -- leaving a real, bookable-looking
    slot on the calendar with no corresponding row in Supabase at all. That
    orphan is invisible to lookup_appointment and change_appointment (both
    query Supabase, not Calendar), and the slot stays permanently blocked
    with no way to manage it through this system.

    The fix: if the Supabase writes fail for any reason, attempt to delete
    the Calendar event that was just created, so a failure rolls back cleanly
    instead of leaving a phantom booking. This can't be made fully atomic
    (the rollback delete call could itself fail), but it converts "silent,
    permanent orphan" into "logged, best-effort cleanup, with a clear error
    message telling you exactly which event ID needs manual attention if the
    rollback itself fails."
    """
    start_dt = datetime.fromisoformat(slot_time)
    end_dt = start_dt + timedelta(hours=1)

    now_local = datetime.now(BUSINESS_TZ)
    if start_dt < now_local:
        raise ValueError("I cannot book appointments for past dates. Please choose a future date.")

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

    event = {
        'summary': f'Consultation: {full_name}',
        'description': f'Phone: {phone}\nEmail: {email}\nCall ID: {vapi_call_id}',
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Karachi'},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Karachi'},
    }
    created_event = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    event_id = created_event.get('id')
    print(f"Google Calendar event created: {created_event.get('htmlLink')}")

    # Everything from here on writes to Supabase. If ANY of it fails, roll
    # back the Calendar event we just created rather than leaving an orphan.
    try:
        existing = supabase.table("contacts").select("id").eq("phone", phone).execute()
        if existing.data:
            contact_id = existing.data[0]["id"]
        else:
            new_contact = supabase.table("contacts").insert({
                "full_name": full_name, "phone": phone, "email": email
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

    except Exception as e:
        print(f"SUPABASE WRITE FAILED after Calendar event {event_id} was created — attempting rollback: {e}")
        try:
            service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
            print(f"Rollback succeeded: deleted orphaned Calendar event {event_id}")
        except Exception as rollback_err:
            print(f"ROLLBACK ALSO FAILED — event {event_id} is orphaned on the calendar and needs MANUAL deletion: {rollback_err}")
        raise ValueError("I had trouble completing that booking. Please try again.")

    return event_id, f"Booking confirmed for {full_name}."


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

    # Safety net: the model has been observed sending a wrong (e.g.
    # training-cutoff default) year. FIX: compute the real current year
    # dynamically rather than hardcoding one.
    if date_filter and len(date_filter) >= 4 and date_filter[:4].isdigit():
        actual_year = str(datetime.now().year)
        if date_filter[:4] != actual_year and int(date_filter[:4]) < datetime.now().year:
            date_filter = actual_year + date_filter[4:]
            print(f"YEAR CORRECTED: {date_filter}")

    if not date_filter:
        return tool_result(call_id, "Please provide a specific date.")

    try:
        local_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
    except ValueError:
        return tool_result(call_id, "I didn't understand that date.")

    start_local = datetime.combine(local_date, time(9, 0), tzinfo=BUSINESS_TZ)
    end_local = datetime.combine(local_date, time(17, 0), tzinfo=BUSINESS_TZ)

    now_local = datetime.now(BUSINESS_TZ)
    if end_local < now_local:
        return tool_result(call_id, "No available slots found for that date. Ask the caller for an alternative date.")

    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc = end_local.astimezone(ZoneInfo("UTC")).isoformat()

    service = get_calendar_service()
    freebusy_body = {"timeMin": start_utc, "timeMax": end_utc, "items": [{"id": GOOGLE_CALENDAR_ID}]}

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
        if not is_busy and current >= now_local:
            available_slots.append({"start": current.isoformat(), "end": slot_end.isoformat()})
        current = slot_end

    if not available_slots:
        return tool_result(call_id, "No available slots found for that date. Ask the caller for an alternative date.")

    # Embed the machine-readable slot_time directly in the speakable result
    # text, for BOTH request shapes (Bug #1 fix).
    lines = []
    for slot in available_slots:
        dt = datetime.fromisoformat(slot['start'])
        local_dt = dt.astimezone(BUSINESS_TZ)
        label = local_dt.strftime("%A, %B %d at %-I:%M %p")
        lines.append(f"Time: {label} | slot_time: {slot['start']}")

    result_text = "Available slots:\n" + "\n".join(lines)
    result_text += (
        "\n\nIMPORTANT: When booking, use the exact value after 'slot_time:' "
        "(the ISO timestamp) as the slot_time argument — never the human-readable "
        "time, never reformat it."
    )

    return tool_result(call_id, result_text)


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
        event_id, result_msg = create_booking_internal(full_name, phone, email, slot_time, vapi_call_id)
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

    contact = supabase.table("contacts").select("id").eq("phone", phone).execute()
    if not contact.data:
        return tool_result(call_id, "No existing bookings found for that phone number.")

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
        return tool_result(call_id, "No upcoming bookings found for that phone number.")

    booking_data = booking.data[0]
    start_dt = datetime.fromisoformat(booking_data["slot_start"])
    local_dt = start_dt.astimezone(BUSINESS_TZ)
    readable_time = local_dt.strftime("%A, %B %d at %-I:%M %p")

    return tool_result(call_id, f"Found booking for {readable_time}.")


# ---- 4. CHANGE APPOINTMENT (create new first, then delete old) ----
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

    # Create the NEW booking FIRST (this now includes its own Calendar/Supabase
    # rollback via create_booking_internal), only delete the old one after the
    # new one is confirmed to exist.
    vapi_call_id = get_call_id(body) or f"change-{uuid.uuid4()}"
    try:
        new_event_id, result_msg = create_booking_internal(full_name, phone, email, new_slot_time, vapi_call_id)
    except ValueError as e:
        return tool_result(call_id, str(e))
    except Exception as e:
        print(f"CHANGE BOOKING ERROR (creating new slot): {e}")
        return tool_result(call_id, "I had trouble booking the new slot, so I've left your original appointment unchanged. Please try again.")

    service = get_calendar_service()
    if old_event_id:
        try:
            service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=old_event_id).execute()
            print(f"Deleted old event: {old_event_id}")
        except Exception as e:
            print(f"WARNING: new booking created but failed to delete OLD Calendar event {old_event_id}: {e}")

    try:
        supabase.table("bookings").delete().eq("id", old_booking_id).execute()
        print(f"Deleted old booking row: {old_booking_id}")
    except Exception as e:
        print(f"WARNING: new booking created but failed to delete OLD Supabase row {old_booking_id}: {e}")

    return tool_result(call_id, f"Appointment changed successfully. {result_msg}")


# ---- 5. ESCALATION ----
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