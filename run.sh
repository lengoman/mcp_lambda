#!/bin/bash
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8080 --workers 1
