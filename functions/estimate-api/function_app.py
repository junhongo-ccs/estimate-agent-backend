import json
import os
import re
import logging
import azure.functions as func
import google.generativeai as genai

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="calculate_estimate", methods=["POST"])
def calculate_estimate(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        screen_count = int(body.get("screen_count", 0))
        complexity = body.get("complexity", "medium")

        multipliers = {"low": 0.8, "medium": 1.0, "high": 1.4}
        m = multipliers.get(complexity, 1.0)

        person_days = round(screen_count * 1.5 * m, 1)

        return func.HttpResponse(
            json.dumps({
                "screen_count": screen_count,
                "complexity": complexity,
                "person_days": person_days
            }),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=400
        )


def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _extract_json(text: str) -> dict:
    """
    LLMが余計なテキストを混ぜても、最初のJSONオブジェクトだけ抜き出す。
    """
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("No JSON object found in model output")
    return json.loads(m.group(0))


def _call_gemini(system: str, user_json: dict) -> dict:
    key = os.getenv("GEMINI_API_KEY")
    desired_model = os.getenv("GEMINI_MODEL")
    # フォールバック（将来切替用ログ）
    if not desired_model:
        desired_model = "gemini-2.5-flash"
        logging.info("model_fallback: GEMINI_MODEL not set -> using gemini-2.5-flash")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        desired_model,
        system_instruction=system
    )
    resp = model.generate_content(
        json.dumps(user_json, ensure_ascii=False),
        generation_config={
            "temperature": 0.2,
            "response_mime_type": "application/json"
        }
    )
    return _extract_json(resp.text)


@app.route(route="enhance_estimate", methods=["POST", "OPTIONS"])
def enhance_estimate(req: func.HttpRequest) -> func.HttpResponse:
    # CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers())

    try:
        payload = req.get_json()
    except Exception:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Invalid JSON"}),
            status_code=400,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    core_result = payload.get("core_result") or {}
    estimated_amount = _safe_int(core_result.get("estimated_amount"), 0)

    if estimated_amount <= 0:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "core_result.estimated_amount is required"}),
            status_code=400,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    # 入力不足判定（低コストガード）
    summary = (payload.get("summary") or "").strip()
    scope = (payload.get("scope") or "").strip()
    if not summary or not scope:
        mult = 1.0
        adjusted_amount = int(estimated_amount * mult)
        body = {
            "status": "ok",
            "adjustment": {
                "multiplier": 1.0,
                "amount_delta": adjusted_amount - estimated_amount,
                "adjusted_amount": adjusted_amount,
                "reasons": ["入力情報が不足しているため、乗数は1.00に固定しました。"],
            },
            "rationale_md": "入力不足により係数調整は行いません。必要情報（要約・範囲）をご提供ください。",
            "added_warnings": ["summary または scope が未入力です"],
            "disclaimer": "本結果は入力内容に基づく補助的な提案です。",
            "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        }
        return func.HttpResponse(
            json.dumps(body, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    system = (
        "Return ONLY valid JSON.\n"
        "Suggest multiplier (1.00-1.30). If insufficient info, 1.00.\n"
        "Schema:{"
        "\"multiplier_suggestion\":number,"
        "\"reasons\":string[],"
        "\"rationale_md\":string,"
        "\"added_warnings\":string[]}"
    )

    try:
        out = _call_gemini(system, payload)
        mult = _clamp(float(out.get("multiplier_suggestion", 1.0)), 1.0, 1.3)
        adjusted_amount = int(estimated_amount * mult)

        body = {
            "status": "ok",
            "adjustment": {
                "multiplier": round(mult, 2),
                "amount_delta": adjusted_amount - estimated_amount,
                "adjusted_amount": adjusted_amount,
                "reasons": out.get("reasons") or []
            },
            "rationale_md": out.get("rationale_md") or "",
            "added_warnings": out.get("added_warnings") or [],
            "disclaimer": "本結果は入力内容に基づく補助的な提案です。",
            "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        }

        return func.HttpResponse(
            json.dumps(body, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": f"LLM call failed: {str(e)}"}, ensure_ascii=False),
            status_code=502,
            mimetype="application/json",
            headers=_cors_headers(),
        )

