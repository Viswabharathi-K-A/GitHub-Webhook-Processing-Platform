import os

import psycopg
from fastapi import FastAPI, HTTPException


app = FastAPI(title="GitHub Webhook Processing Platform")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
def database_health():
    try:
        with psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=3,
            options="-c statement_timeout=3000",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise HTTPException(
                        status_code=503, detail="Database unavailable"
                    )
    except (psycopg.Error, KeyError):
        raise HTTPException(
            status_code=503, detail="Database unavailable"
        ) from None

    return {"status": "ok"}
