import os
import json
import hmac
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from db import supabase

app = FastAPI()

VAPI_SERVER_SECRET = os.environ["VAPI_SERVER_SECRET"]
BUSINESS_TZ = ZoneInfo("Asia/Karachi")


def verify_secret(request: Request) -> bool:
    received = request.headers.get("X-Vapi-Secret")
    if not received:
        return False
    return hmac.compare_digest(received, VAPI_SERVER_SECRET)


def log_payload(endpoint: str, body: dict):
    print(f"RAW VAPI PAYLOAD [{endpoint}]:", body)


def get_tool_call(body: dict):
    """
    Extract tool call ID and arguments from either:
    - Enveloped shape: { "message": { "type": "tool-calls", "toolCalls": [...] } }
    - Flat shape: { "date": "2026-09-10", ... }
    """
    message = body.get("message")
    if isinstance(message, dict) and message.get("type") == "tool-calls":
        tool_calls = message.get("toolCalls", [])
        if not tool_calls:
            return None, None
        
        call = tool_calls[0]
        
        # CRITICAL FIX: arguments are nested inside the 'function' key!
        func = call.get("function", {})
        raw_args = func.get("arguments", call.get("parameters", {}))
        
        # Vapi often sends arguments as a JSON string, not a dict
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        else:
            args = raw_args if isinstance(raw_args, dict) else {}
        
        return call.get("id"), args

    # Fallback: flat shape (the body itself IS the arguments)
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


# ---- 1. Check availability ----
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

    query = supabase.table("availability").select("*").eq("is_booked", False)
    if date_filter:
        try:
            local_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
        except ValueError:
            return tool_result(call_id, "I didn't understand that date. Please ask the caller to repeat it clearly.")

        day_start_local = datetime.combine(local_date, time.min, tzinfo=BUSINESS_TZ)
        day_end_local = datetime.combine(local_date, time.max, tzinfo=BUSINESS_TZ)
        query = query.gte("slot_start", day_start_local.isoformat()).lte("slot_start", day_end_local.isoformat())

    result = query.order("slot_start").limit(3).execute()
    slots = result.data

    if not slots:
        return tool_result(call_id, "No available slots found for that date. Ask the caller for an alternative date.")

    slot_list = []
    for s in slots:
        utc_dt = datetime.fromisoformat(s["slot_start"])
        local_dt = utc_dt.astimezone(BUSINESS_TZ)
        label = local_dt.strftime("%A, %B %d at %-I:%M %p")
        slot_list.append(f"ID: {s['id']} | Time: {label}")

    result_text = "Available slots:\n" + "\n".join(slot_list)
    result_text += "\n\nIMPORTANT: When booking, use the exact ID value (not the time string) as slot_id."

    return tool_result(call_id, result_text)


# ---- 2. Book appointment (atomic, race-condition safe) ----
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

    # Strip whitespace to handle LLM copy-paste errors
    slot_id = str(args.get("slot_id", "")).strip()
    full_name = str(args.get("full_name", "")).strip()
    phone = str(args.get("phone", "")).strip()
    email = str(args.get("email", "")).strip()

    if not slot_id or not full_name or not phone:
        return tool_result(call_id, "Missing required booking details. Ask the caller to repeat their name and phone number.")

    # Validate slot_id looks like a UUID (basic sanity check)
    try:
        uuid.UUID(slot_id)
    except ValueError:
        return tool_result(call_id, "Invalid slot ID format. Please try booking again with the correct slot ID from the available times.")

    # Atomic claim — prevents double-booking
    claim = supabase.table("availability") \
        .update({"is_booked": True}) \
        .eq("id", slot_id).eq("is_booked", False) \
        .execute()

    if not claim.data:
        return tool_result(call_id, "That slot was just taken. Please offer the caller alternative times.")

    # Upsert contact (dedupe by phone)
    existing = supabase.table("contacts").select("id").eq("phone", phone).execute()
    if existing.data:
        contact_id = existing.data[0]["id"]
    else:
        new_contact = supabase.table("contacts").insert({
            "full_name": full_name, "phone": phone, "email": email
        }).execute()
        contact_id = new_contact.data[0]["id"]

    # Insert booking
    supabase.table("bookings").insert({
        "contact_id": contact_id,
        "slot_id": slot_id,
        "call_id": vapi_call_id
    }).execute()

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

    supabase.table("call_logs").upsert({
        "call_id": vapi_call_id,
        "outcome": "escalated",
        "escalation_reason": reason
    }).execute()

    return tool_result(call_id, "Escalation logged. Offer the caller a callback.")