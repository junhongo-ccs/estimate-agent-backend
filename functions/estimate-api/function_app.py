import json
import azure.functions as func

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
