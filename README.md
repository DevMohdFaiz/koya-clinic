# Koya Clinic API

A lightweight clinic appointment booking API and patient web interface built with FastAPI and PostgreSQL.
## Features

- **Level 1**: List slots, filter by provider, book appointment slots, and look up bookings.
- **Level 2**: Timing-safe `X-API-Key` authentication and input validation (trimmed names, lowercased emails, date checks).
- **Level 3**: Concurrency safety via PostgreSQL partial unique index (`one_confirmed_per_slot`) to eliminate race conditions; slot cancellation.
- **Level 4**: Safe retries with `Idempotency-Key` headers; cursor-based pagination for bookings.
- **Level 5**: Per-email rate limiting (max 5 booking attempts/minute) returning `429 Too Many Requests` with a `Retry-After` header.
- **Web App**: Minimal UI at `/` allowing patients to view slots, book appointments, and see confirmations.

## Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **Database**: PostgreSQL (Neon) via `psycopg`
- **Deployment**: Vercel

## Environment Variables

Create a `.env` file in the project root:

```env
API_KEY=your_api_key_here
DATABASE_URL=postgresql://user:password@host/neondb?sslmode=require
```

## Getting Started

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Run Locally

```bash
uvicorn main:app --reload
```

The web interface will be available at `http://localhost:8000/`.


## API Endpoints

All endpoints (except `/`) require the `X-API-Key` header.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web application interface |
| `POST` | `/reset` | Replaces all slots and resets test data |
| `GET` | `/slots` | List all slots (supports `?provider=...`) |
| `POST` | `/bookings` | Book a slot (supports `Idempotency-Key` header) |
| `GET` | `/bookings` | List bookings by email with cursor pagination (`?email=...&limit=...&cursor=...`) |
| `GET` | `/bookings/{id}` | Look up a specific booking |
| `DELETE` | `/bookings/{id}` | Cancel a booking and free the slot |
