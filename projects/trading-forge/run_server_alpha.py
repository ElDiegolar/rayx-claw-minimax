#!/usr/bin/env python3
"""
Trading Forge Server Runner (Alpha Engine Edition)

Simple script to start the FastAPI server with uvicorn using AlphaEngine.

Usage:
    python run_server_alpha.py [--host HOST] [--port PORT] [--reload]
    python run_server.py --engine alpha  # Use alpha engine
    python run_server.py --engine legacy # Use legacy orchestrator

Options:
    --host HOST    Host to bind to (default: 0.0.0.0)
    --port PORT    Port to bind to (default: 8000)
    --reload       Enable auto-reload for development
    --engine ENGINE  Which engine to use: 'alpha' or 'legacy' (default: alpha)
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
    parser.add_argument(
        "--engine",
        default="alpha",
        choices=["alpha", "legacy"],
        help="Which engine to use: 'alpha' or 'legacy' (default: alpha)"
    )
    
    args = parser.parse_args()
    
    # Select the server module
    if args.engine == "alpha":
        server_module = "server_alpha:app"
        engine_name = "Alpha Engine"
    else:
        server_module = "server:app"
        engine_name = "Legacy Orchestrator"
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   Trading Forge API Server ({engine_name:15})         ║
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
║   - POST /stop           Stop trading                       ║
║   - POST /markets        Add market                         ║
║   - GET  /trades         Trade history                       ║
║   - GET  /dropped-signals Dropped signals                   ║
║   - GET  /config         Get configuration                  ║
║   - PATCH /config        Update configuration               ║
║   - GET  /engine/status  AlphaEngine status (alpha only)   ║
║   - GET  /engine/state   AlphaEngine state (alpha only)     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    uvicorn.run(
        server_module,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=True,
    )


if __name__ == "__main__":
    main()
