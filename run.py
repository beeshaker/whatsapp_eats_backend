# app.py  (or run.py / main.py — whatever you call it)

from flask import Flask
from dotenv import load_dotenv, find_dotenv
from whatsapp_bot.bot_blueprint import bp as wa_bp

# Load .env (keep this)
load_dotenv(find_dotenv(), override=True)

def create_app():
    app = Flask(__name__)

    # CRITICAL FIXES:
    app.url_map.strict_slashes = False                      # Stops 308 redirects
    app.register_blueprint(wa_bp)                           # NO url_prefix !!

    @app.get("/")
    @app.get("/healthz")
    def health():
        return {"status": "ok", "bot": "QuickBite WhatsApp Bot Running"}, 200

    return app


if __name__ == "__main__":
    app = create_app()
    # Use port 8080 to match your ngrok / Cloudflare tunnel
    app.run(host="0.0.0.0", port=8080, debug=True)