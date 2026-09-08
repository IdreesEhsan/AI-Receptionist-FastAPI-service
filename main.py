import os
import hmac
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from db import supabase

app = FastAPI()

VAPI_SERVER_SECRET = os.environ["VAPI_SERVER_SECRET"]

# Match whatever timezone your Supabase seed data was built in.
BUSINESS_TZ = ZoneInfo("Asia/Karachi")


def verify_secret(request: Request) -> bool:
    """Vapi sends back whatever secret you configured on each Tool's Server URL,
    in the X-Vapi-Secret header, on every request to that tool. This is a plain
    shared-secret check, not a cryptographic signature like Retell's — so there's
    no regex/library call here that can crash on a missing header the way
    Retell's SDK did. We still check for a missing header explicitly and fail
    closed (return False) rather than relying on that being safe by accident.
    hmac.compare_digest is used instead of `==` for a timing-safe comparison."""
    received = request.headers.get("X-Vapi-Secret")
    if not received:
        return False
    return hmac.compare_digest(received, VAPI_SERVER_SECRET)


def log_payload(endpoint: str, body: dict):
    """Temporary debug logging — prints the raw request body to Railway's logs
    so you can confirm the actual shape Vapi sends for EACH tool (book_appointment
    and escalate haven't been verified live yet, only check_availability has).
    Tagged with the endpoint name so the three tools don't get mixed up in the
    log stream. Remove this once all three tools are confirmed working end-to-end
    — it logs caller PII (name/phone/email) to Railway's logs, which you don't
    want lingering in production."""
    print(f"RAW VAPI PAYLOAD [{endpoint}]:", body)


def get_tool_call(body: dict):
    """Pull the arguments out of the request body.

    CONFIRMED FROM PRODUCTION LOGS (Sept 2026): this Vapi account's Custom
    Tool sends a FLAT body — just the arguments dict, e.g. {"date": "2024-09-10"}
    — with no {"message": {"type": "tool-calls", ...}} envelope and no
    toolCallId anywhere in the body. This differs from Vapi's documented
    Function-tool webhook shape (and from Retell's), so we treat the whole
    body as the arguments and correlate by the HTTP request/response pair
    itself rather than by an echoed id.

    We still check for the older enveloped shape first, in case Vapi changes
    this or a different tool/account sends it — so this keeps working either
    way instead of silently breaking again."""
    message = body.get("message")
    if isinstance(message, dict) and message.get("type") == "tool-calls":
        tool_calls = message.get("toolCallList", [])
        if not tool_calls:
            return None, None
        call = tool_calls[0]
        args = call.get("arguments", call.get("parameters", {}))
        return call.get("id"), args

    # Flat shape: the body itself IS the arguments. No id to extract, so
    # return a sentinel so downstream code knows we're in flat mode.
    if isinstance(body, dict):
        return "flat", body

    return None, None


def tool_result(tool_call_id, result_text: str):
    """Shape the response to match whichever request shape we received.
    Flat requests (the common case in production, per the logs) get a flat
    response back — Vapi reads the result directly, no toolCallId needed.
    Enveloped requests get the documented {"results": [...]} shape."""
    if tool_call_id == "flat":
        return {"result": result_text}
    return {"results": [{"toolCallId": tool_call_id, "result": result_text}]}


def get_call_id(body: dict):
    """The phone call's own id, used to tie bookings/escalations back to a
    call in Supabase. Only present in the enveloped shape — flat requests
    carry no call id at all, so we fall back to None (call_id is nullable
    in the bookings/call_logs schema)."""
    message = body.get("message")
    if isinstance(message, dict):
        call = message.get("call", {})
        return call.get("id")
    return None


@app.get("/")
async def health_check():
    # Hit this first after every deploy — confirms the service is actually up
    # before you spend time debugging anything downstream.
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

    date_filter = args.get("date")  # e.g. "2026-09-10", meant as a LOCAL calendar date

    query = supabase.table("availability").select("*").eq("is_booked", False)
    if date_filter:
        # Build local-day boundaries in BUSINESS_TZ, then convert to UTC for the query —
        # a "day" in Lahore doesn't line up with a UTC day, so filtering on raw date
        # strings against UTC timestamps silently returns the wrong slots near day edges.
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

    # Convert stored UTC timestamps back to local time and format as something speakable.
    readable = []
    for s in slots:
        utc_dt = datetime.fromisoformat(s["slot_start"])
        local_dt = utc_dt.astimezone(BUSINESS_TZ)
        # %-I drops the leading zero on the hour; Linux/Mac only (fine on Render/Railway).
        readable.append(local_dt.strftime("%A, %B %d at %-I:%M %p"))

    return tool_result(call_id, f"Available slots: {'; '.join(readable)}")


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

    vapi_call_id = get_call_id(body)  # the actual phone call, if present; None in flat mode

    slot_id = args.get("slot_id")
    full_name = args.get("full_name")
    phone = args.get("phone")
    email = args.get("email")

    if not slot_id or not full_name or not phone:
        return tool_result(call_id, "Missing required booking details. Ask the caller to repeat their name and phone number.")

    # Atomic claim — prevents double-booking, safe even if Vapi retries this call
    claim = supabase.table("availability") \
        .update({"is_booked": True}) \
        .eq("id", slot_id).eq("is_booked", False) \
        .execute()

    if not claim.data:
        return tool_result(call_id, "That slot was just taken. Please offer the caller alternative times.")

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
        # call_logs.call_id is NOT NULL + unique — flat-mode requests carry no
        # real call id, so fabricate a placeholder rather than let the insert
        # crash. This row just won't tie back to a real call in Supabase.
        vapi_call_id = f"unknown-{uuid.uuid4()}"
    reason = args.get("reason", "unspecified")

    supabase.table("call_logs").upsert({
        "call_id": vapi_call_id,
        "outcome": "escalated",
        "escalation_reason": reason
    }).execute()

    return tool_result(call_id, "Escalation logged. Offer the caller a callback.")