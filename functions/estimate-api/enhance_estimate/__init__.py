import json
import os
import re
import azure.functions as func

# OpenAI SDK v1
from openai import OpenAI


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


def main(req: func.HttpRequest) -> func.HttpResponse:
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

    project_name = (payload.get("project_name") or "").strip()
    summary = (payload.get("summary") or "").strip()
    scope = (payload.get("scope") or "").strip()
    core_result = payload.get("core_result") or {}

    estimated_amount = _safe_int(core_result.get("estimated_amount"), 0)
    currency = (core_result.get("currency") or "JPY").strip()
    breakdown = core_result.get("breakdown") or {}
    warnings = core_result.get("warnings") or []
    assumptions = core_result.get("assumptions") or []
    config_version = core_result.get("config_version")

    if estimated_amount <= 0:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "core_result.estimated_amount is required"}),
            status_code=400,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    # OpenAI settings
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    if not api_key:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "OPENAI_API_KEY is not set"}),
            status_code=500,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    client = OpenAI(api_key=api_key)

    # Prompt: JSON only, suggest multiplier 1.00-1.30
    system = (
        "You are an estimation assistant. "
        "Return ONLY valid JSON. No markdown, no extra text. "
        "Your job: suggest a multiplier (1.00 to 1.30) and concise reasons, plus rationale_md.\n"
        "Rules:\n"
        "- If information is insufficient, set multiplier_suggestion to 1.00.\n"
        "- Reasons must be short Japanese sentences.\n"
        "- rationale_md must be short and explainable for business.\n"
        "- added_warnings is optional.\n"
        "Output schema:\n"
        "{"
        "\"multiplier_suggestion\": number,"
        "\"reasons\": string[],"
        "\"rationale_md\": string,"
        "\"added_warnings\": string[]"
        "}"
    )

    user = {
        "project_name": project_name,
        "summary": summary,
        "scope": scope,
        "core_result": {
            "estimated_amount": estimated_amount,
            "currency": currency,
            "breakdown": breakdown,
            "warnings": warnings,
            "assumptions": assumptions,
            "config_version": config_version,
        },
    }

    try:
        # chat.completions style
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        out = _extract_json(text)

        mult = float(out.get("multiplier_suggestion", 1.0))
        mult = _clamp(mult, 1.0, 1.3)

        adjusted_amount = int(estimated_amount * mult)
        amount_delta = adjusted_amount - estimated_amount

        reasons = out.get("reasons") or []
        rationale_md = out.get("rationale_md") or ""
        added_warnings = out.get("added_warnings") or []

        body = {
            "status": "ok",
            "adjustment": {
                "multiplier": round(mult, 2),
                "amount_delta": amount_delta,
                "adjusted_amount": adjusted_amount,
                "reasons": reasons,
            },
            "rationale_md": rationale_md,
            "added_warnings": added_warnings,
            "disclaimer": "本結果は入力内容に基づく補助的な提案です。",
            "model": model,
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
