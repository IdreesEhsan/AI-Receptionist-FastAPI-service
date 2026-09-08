import os
import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from retell import Retell

from db import supabase

app = FastAPI()
retell = Retell(api_key=os.environ["RETELL_API_KEY"])

# Match whatever timezone your Supabase seed data was built in.
BUSINESS_TZ = ZoneInfo("Asia/Karachi")


# ---------------------------------------------------------------------------
# Shared helper: verify Retell's signature, return parsed body (or None, None)
# ---------------------------------------------------------------------------
async def verify_and_parse(request: Request):
    raw_body = (await request.body()).decode("utf-8")
    valid = retell.verify(
        raw_body,
        api_key=os.environ["RETELL_API_KEY"],
        signature=request.headers.get("X-Retell-Signature"),
    )
    if not valid:
        return None, None
    return json.loads(raw_body), raw_body


@app.get("/")
async def health_check():
    # Simple endpoint to confirm the deploy is alive — hit this first after every deploy.
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 1. Check availability
# ---------------------------------------------------------------------------
@app.post("/check-availability")
async def check_availability(request: Request):
    body, _ = await verify_and_parse(request)
    if body is None:
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    params = body["args"]
    date_filter = params.get("date")  # e.g. "2026-09-10", meant as a LOCAL calendar date

    query = supabase.table("availability").select("*").eq("is_booked", False)

    if date_filter:
        # Build local-day boundaries in BUSINESS_TZ, then convert to UTC for the query.
        # A "day" in Lahore doesn't line up with a UTC day — filtering on raw date
        # strings against UTC timestamps silently returns wrong slots near day edges.
        try:
            local_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
        except ValueError:
            return {"result": "I didn't understand that date. Please ask the caller to repeat it clearly."}

        day_start_local = datetime.combine(local_date, time.min, tzinfo=BUSINESS_TZ)
        day_end_local = datetime.combine(local_date, time.max, tzinfo=BUSINESS_TZ)
        query = query.gte("slot_start", day_start_local.isoformat()).lte(
            "slot_start", day_end_local.isoformat()
        )

    result = query.order("slot_start").limit(3).execute()
    slots = result.data

    if not slots:
        return {"result": "No available slots found for that date. Ask the caller for an alternative date."}

    # Convert stored UTC timestamps back to local time and format as something speakable.
    readable = []
    for s in slots:
        utc_dt = datetime.fromisoformat(s["slot_start"])
        local_dt = utc_dt.astimezone(BUSINESS_TZ)
        # %-I drops the leading zero on the hour; Linux/Mac only (fine on Render/Railway).
        readable.append(local_dt.strftime("%A, %B %d at %-I:%M %p"))

    return {
        "result": f"Available slots: {'; '.join(readable)}",
        "slot_ids": [s["id"] for s in slots],
    }


# ---------------------------------------------------------------------------
# 2. Book appointment (atomic claim — race-condition safe)
# ---------------------------------------------------------------------------
@app.post("/book-appointment")
async def book_appointment(request: Request):
    body, _ = await verify_and_parse(request)
    if body is None:
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    params = body["args"]
    call_id = body["call"]["call_id"]

    slot_id = params.get("slot_id")
    full_name = params.get("full_name")
    phone = params.get("phone")
    email = params.get("email")

    if not slot_id or not full_name or not phone:
        return {"result": "Missing required booking details. Ask the caller to repeat their name and phone number."}

    # Atomic claim: only succeeds if the slot is still unbooked. Prevents double-booking
    # even if two calls race for the same slot, or Retell retries this request.
    claim = (
        supabase.table("availability")
        .update({"is_booked": True})
        .eq("id", slot_id)
        .eq("is_booked", False)
        .execute()
    )

    if not claim.data:
        return {"result": "That slot was just taken. Please offer the caller alternative times."}

    # Upsert contact by phone
    existing = supabase.table("contacts").select("id").eq("phone", phone).execute()
    if existing.data:
        contact_id = existing.data[0]["id"]
    else:
        new_contact = (
            supabase.table("contacts")
            .insert({"full_name": full_name, "phone": phone, "email": email})
            .execute()
        )
        contact_id = new_contact.data[0]["id"]

    supabase.table("bookings").insert(
        {"contact_id": contact_id, "slot_id": slot_id, "call_id": call_id}
    ).execute()

    return {"result": f"Booking confirmed for {full_name}."}


# ---------------------------------------------------------------------------
# 3. Escalation logging
# ---------------------------------------------------------------------------
@app.post("/escalate")
async def escalate(request: Request):
    body, _ = await verify_and_parse(request)
    if body is None:
        return JSONResponse(status_code=401, content={"message": "Unauthorized"})

    call_id = body["call"]["call_id"]
    reason = body["args"].get("reason", "unspecified")

    supabase.table("call_logs").upsert(
        {"call_id": call_id, "outcome": "escalated", "escalation_reason": reason}
    ).execute()

    return {"result": "Escalation logged. Offer the caller a callback."}