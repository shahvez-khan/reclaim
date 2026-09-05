"""
Centralized configuration (Phase 4.1 of the production-hardening loop).

Every value that a real deployment would need to change lives here, read
from environment variables (with a .env file loaded if present) rather than
hardcoded across the codebase. Every existing hardcoded constant this
replaces (COOLDOWN_HOURS=24, MAX_ATTEMPTS=3, model paths, DB path) keeps its
exact original default, so behavior is unchanged unless someone explicitly
overrides via environment/.env — this is a refactor, not a behavior change.

RAZORPAY_API_KEY / RAZORPAY_WEBHOOK_SECRET are read here but UNUSED today —
see backend/execution.py's PaymentExecutor seam. Structuring config to
already have a slot for them is what makes that seam a real, defined
integration point instead of hand-waved.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if no .env file present — safe to call unconditionally
except ImportError:
    pass  # python-dotenv is optional; env vars still work without it

PROJECT_ROOT = Path(__file__).parent.parent


# --- Database ---
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/revenue_recovery.db")
# Kept for the existing sqlite3-based schema.py, which uses a filesystem path
# directly rather than a full SQLAlchemy URL. See backend/db.py for the
# documented Postgres migration path.
DB_PATH = PROJECT_ROOT / "data" / "revenue_recovery.db"

# --- API server ---
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8000"))

# --- Policy / decision engine (same defaults as the original hardcoded constants) ---
COOLDOWN_HOURS = int(os.environ.get("COOLDOWN_HOURS", "24"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
SAFETY_ITERATION_CAP = int(os.environ.get("SAFETY_ITERATION_CAP", "6"))

# --- ML model paths ---
MODEL_PATH = PROJECT_ROOT / os.environ.get("MODEL_PATH", "models/recovery_model.pkl")
MODEL_METADATA_PATH = PROJECT_ROOT / os.environ.get("MODEL_METADATA_PATH", "models/model_metadata.json")
MODEL_SCALER_PATH = PROJECT_ROOT / os.environ.get("MODEL_SCALER_PATH", "models/feature_scaler.pkl")

# --- Rate limiting ---
RUN_BATCH_RATE_LIMIT_MAX = int(os.environ.get("RUN_BATCH_RATE_LIMIT_MAX", "3"))
RUN_BATCH_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RUN_BATCH_RATE_LIMIT_WINDOW_SECONDS", "60"))

# --- Logging ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_DIR = PROJECT_ROOT / os.environ.get("LOG_DIR", "logs")

# --- Payment execution ---
RAZORPAY_API_KEY = os.environ.get("RAZORPAY_API_KEY", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
PAYMENT_EXECUTOR = os.environ.get("PAYMENT_EXECUTOR", "mock")
