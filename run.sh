#!/bin/bash


# Fix PYTHONPATH to include layer
export PYTHONPATH=$PYTHONPATH:/opt/python

exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8080 --workers 1
