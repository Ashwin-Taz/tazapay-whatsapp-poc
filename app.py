"""
Tazapay WhatsApp POC
--------------------
WhatsApp (Twilio) → Flask webhook → Claude AI + Tazapay tools → WhatsApp reply
"""

import os
import json
import logging
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config (set via environment variables or .env)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")  # Twilio sandbox default
TAZAPAY_API_KEY = os.getenv("TAZAPAY_API_KEY", "")
TAZAPAY_API_SECRET = os.getenv("TAZAPAY_API_SECRET", "")
TAZAPAY_BASE_URL = os.getenv("TAZAPAY_BASE_URL", "https://api.tazapay.com")

# Authorized WhatsApp numbers (E.164 format, e.g. whatsapp:+6512345678)
# Leave empty to allow all numbers during POC
AUTHORIZED_NUMBERS = os.getenv("AUTHORIZED_NUMBERS", "").split(",") if os.getenv("AUTHORIZED_NUMBERS") else []

# ---------------------------------------------------------------------------
# In-memory conversation history (per phone number)
# ---------------------------------------------------------------------------
conversation_store: dict[str, list] = {}

# ---------------------------------------------------------------------------
# Tazapay API helpers
# ---------------------------------------------------------------------------
import requests
import base64
import hashlib
import hmac
import time

def tazapay_headers():
    """Generate Tazapay API authentication headers."""
    timestamp = str(int(time.time()))
    message = timestamp + TAZAPAY_API_KEY
    signature = hmac.new(
        TAZAPAY_API_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": f"Basic {base64.b64encode(f'{TAZAPAY_API_KEY}:{TAZAPAY_API_SECRET}'.encode()).decode()}",
        "Content-Type": "application/json",
    }

def tazapay_get(path: str) -> dict:
    url = f"{TAZAPAY_BASE_URL}{path}"
    resp = requests.get(url, headers=tazapay_headers(), timeout=15, verify=False)
    resp.raise_for_status()
    return resp.json()

def tazapay_post(path: str, payload: dict) -> dict:
    url = f"{TAZAPAY_BASE_URL}{path}"
    resp = requests.post(url, headers=tazapay_headers(), json=payload, timeout=15, verify=False)
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "check_balance",
        "description": (
            "Fetch the merchant's Tazapay wallet balances. "
            "Optionally filter by a specific currency (e.g. USD, SGD). "
            "Returns all balances if no currency is specified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "currency": {
                    "type": "string",
                    "description": "Optional 3-letter ISO currency code (e.g. USD). Leave empty for all balances.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_fx_rate",
        "description": "Get the Tazapay FX rate between two currencies for a given amount.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string", "description": "Source currency (e.g. USD)"},
                "to_currency": {"type": "string", "description": "Target currency (e.g. INR)"},
                "amount": {"type": "number", "description": "Amount in source currency"},
            },
            "required": ["from_currency", "to_currency", "amount"],
        },
    },
    {
        "name": "create_payment_link",
        "description": "Generate a Tazapay checkout payment link to collect funds from a customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "customer_email": {"type": "string"},
                "customer_country": {"type": "string", "description": "ISO alpha-2 country code"},
                "amount": {"type": "number"},
                "currency": {"type": "string", "description": "Invoice currency (e.g. USD)"},
                "description": {"type": "string"},
            },
            "required": ["customer_name", "customer_email", "customer_country", "amount", "currency", "description"],
        },
    },
    {
        "name": "check_payout_status",
        "description": "Check the status of a Tazapay payout by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payout_id": {"type": "string", "description": "Tazapay payout ID (starts with po_)"},
            },
            "required": ["payout_id"],
        },
    },
]

SYSTEM_PROMPT = """You are a Tazapay payment assistant for merchants, accessible via WhatsApp.

You help merchants with:
1. Checking their Tazapay wallet balances
2. Getting FX rates between currencies
3. Creating payment links to collect funds from customers
4. Checking payout statuses

Keep responses concise and WhatsApp-friendly (no markdown, use plain text and emojis sparingly).
Always confirm important details before taking action.
If a request is ambiguous, ask a clarifying question.
For payment links, always confirm the details before creating.

Format numbers clearly: use commas for thousands, 2 decimal places for amounts.
"""

# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------
def execute_tool(tool_name: str, tool_input: dict) -> str:
    logger.info(f"Executing tool: {tool_name} with input: {tool_input}")
    try:
        if tool_name == "check_balance":
            currency = tool_input.get("currency", "").upper()
            # Try multiple possible endpoint paths
            for path in ["/v2/balance", "/v1/balance", "/v2/account/balance"]:
                try:
                    data = tazapay_get(path + (f"?currency={currency}" if currency else ""))
                    break
                except Exception:
                    continue
            # Parse response — handle various response shapes
            raw = data.get("data", data)
            if isinstance(raw, dict) and "balances" in raw:
                raw = raw["balances"]
            if isinstance(raw, dict):
                active = {k: v for k, v in raw.items() if float(v or 0) != 0}
                zero = [k for k, v in raw.items() if float(v or 0) == 0]
                if active:
                    lines = [f"{k}: {v}" for k, v in active.items()]
                    result = "Active balances:\n" + "\n".join(lines)
                    if zero:
                        result += f"\nZero: {', '.join(zero)}"
                else:
                    result = "All balances are zero."
            else:
                result = str(raw)
            return result

        elif tool_name == "get_fx_rate":
            fc = tool_input["from_currency"].upper()
            tc = tool_input["to_currency"].upper()
            amt = int(tool_input["amount"])
            data = tazapay_get(f"/v2/fx?from={fc}&to={tc}&amount={amt}")
            raw = data.get("data", data)
            rate = raw.get("rate") if isinstance(raw, dict) else data.get("rate")
            converted = raw.get("converted_amount") if isinstance(raw, dict) else data.get("converted_amount")
            return f"FX Rate: 1 {fc} = {rate} {tc}\n{amt} {fc} = {converted} {tc}"

        elif tool_name == "create_payment_link":
            payload = {
                "customer_details": {
                    "name": tool_input["customer_name"],
                    "email": tool_input["customer_email"],
                    "country": tool_input["customer_country"],
                },
                "invoice_currency": tool_input["currency"].upper(),
                "amount": tool_input["amount"],
                "transaction_description": tool_input["description"],
                "success_url": "https://tazapay.com/success",
                "cancel_url": "https://tazapay.com/cancel",
            }
            data = tazapay_post("/v2/session/checkout", payload)
            raw = data.get("data", data)
            url = raw.get("url") or raw.get("payment_url") or raw.get("checkout_url", "N/A")
            session_id = raw.get("id", "N/A")
            return f"Payment link created!\nURL: {url}\nSession ID: {session_id}"

        elif tool_name == "check_payout_status":
            payout_id = tool_input["payout_id"]
            data = tazapay_get(f"/v2/payout/{payout_id}")
            payout = data.get("data", data)
            status = payout.get("status", "unknown")
            amount = payout.get("amount", "N/A")
            currency = payout.get("currency", "")
            beneficiary = payout.get("beneficiary_name") or payout.get("beneficiary", {}).get("name", "N/A")
            created = payout.get("created_at", "N/A")
            return (
                f"Payout {payout_id}\n"
                f"Status: {status.upper()}\n"
                f"Amount: {amount} {currency}\n"
                f"Beneficiary: {beneficiary}\n"
                f"Created: {created}"
            )

        else:
            return f"Unknown tool: {tool_name}"

    except requests.HTTPError as e:
        logger.error(f"Tazapay API error: {e.response.text}")
        return f"Tazapay API error: {e.response.status_code} - {e.response.text[:200]}"
    except Exception as e:
        logger.error(f"Tool error: {e}")
        return f"Error executing {tool_name}: {str(e)}"

# ---------------------------------------------------------------------------
# Claude agentic loop
# ---------------------------------------------------------------------------
def run_claude(phone_number: str, user_message: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Get or init conversation history
    history = conversation_store.get(phone_number, [])
    history.append({"role": "user", "content": user_message})

    max_iterations = 5
    for _ in range(max_iterations):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

        # Add assistant response to history
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract text response
            text = " ".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            conversation_store[phone_number] = history[-20:]  # Keep last 20 turns
            return text.strip()

        elif response.stop_reason == "tool_use":
            # Execute all tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            history.append({"role": "user", "content": tool_results})

        else:
            break

    conversation_store[phone_number] = history[-20:]
    return "Sorry, I couldn't complete that request. Please try again."

# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    logger.info(f"Incoming message from {from_number}: {body}")

    # Authorization check
    if AUTHORIZED_NUMBERS and from_number not in AUTHORIZED_NUMBERS:
        logger.warning(f"Unauthorized number: {from_number}")
        resp = MessagingResponse()
        resp.message("Sorry, you are not authorized to use this service.")
        return str(resp)

    if not body:
        resp = MessagingResponse()
        resp.message("Hi! I'm your Tazapay assistant. You can ask me to:\n- Check balance\n- Get FX rate\n- Create payment link\n- Check payout status")
        return str(resp)

    # Handle reset command
    if body.lower() in ["/reset", "reset", "clear"]:
        conversation_store.pop(from_number, None)
        resp = MessagingResponse()
        resp.message("Conversation reset. How can I help you?")
        return str(resp)

    # Run Claude
    reply = run_claude(from_number, body)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "tazapay-whatsapp-poc"}, 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Tazapay WhatsApp POC on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
