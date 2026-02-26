#!/bin/bash

# Rayx-Claw-Final Start Script with Auto-Port Detection
cd "$(dirname "$0")"

START_PORT=${1:-8080}
PORT=$START_PORT

is_port_free() {
    ! lsof -i :"$1" >/dev/null 2>&1
}

MAX_PORT=9000
while ! is_port_free "$PORT"; do
    echo "Port $PORT is in use, trying next..."
    PORT=$((PORT + 1))
    if [ "$PORT" -gt "$MAX_PORT" ]; then
        echo "No free ports found between $START_PORT and $MAX_PORT."
        exit 1
    fi
done

echo "Starting Rayx-Claw-Final on port $PORT..."

if [ ! -d "venv" ] && [ ! -d ".venv" ]; then
    echo "Installing Python dependencies..."
    pip3 install -r requirements.txt
fi

echo "Server running at http://localhost:$PORT"
python3 -m uvicorn server:app --host 0.0.0.0 --port "$PORT"
