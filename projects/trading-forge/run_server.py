#!/usr/bin/env python3
"""
Trading Forge Server Runner

Simple script to start the FastAPI server with uvicorn.

Usage:
    python run_server.py [--host HOST] [--port PORT] [--reload]

Options:
    --host HOST    Host to bind to (default: 0.0.0.0)
    --port PORT    Port to bind to (default: 8000)
    --reload       Enable auto-reload for development
"""

import sys
import argparse

# Add project path
sys.path.insert(0, '/home/ldfla/workspace/rayx-claw-final/projects/trading-forge')

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Start Trading Forge API server")
    parser.add_argument(
        "--host", 
        default="0.0.0.0", 
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=8000, 
        help="Port to bind to (default: 8000)"
    )
    parser.add_argument(
        "--reload", 
        action="store_true", 
        help="Enable auto-reload for development"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Logging level (default: info)"
    )
    
    args = parser.parse_args()
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   Trading Forge API Server                                   ║
║                                                              ║
║   Starting server on {args.host}:{args.port}                       ║
║                                                              ║
║   Endpoints:                                                 ║
║   - GET  /health         Health check                       ║
║   - GET  /status         Trading status                     ║
║   - GET  /portfolio      Portfolio details                 ║
║   - GET  /positions      Current positions                 ║
║   - GET  /metrics        Trading metrics                    ║
║   - POST /start           Start trading                      ║
║   - POST /stop           Stop trading                        ║
║   - POST /markets        Add market                         ║
║   - GET  /trades         Trade history                       ║
║   - GET  /dropped-signals Dropped signals                   ║
║   - GET  /config         Get configuration                  ║
║   - PATCH /config        Update configuration               ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=True,
    )


if __name__ == "__main__":
    main()
