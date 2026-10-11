"""Debian service entry point. TLS validation happens before mining sockets open."""
import argparse
import asyncio
import json
import signal
from pathlib import Path
from aiohttp import web
from . import __version__
from .control import validate_service_config,tls_context,create_control_app
from .state import AtomicState
from .runtime import RouterRuntime
from .metrics import Metrics,DifficultyConvention
from .multi import MultiRouter,create_multi_app


async def serve(config):
    ssl_context=tls_context(config)
    conventions=dict(config.get('algorithm_difficulty_conventions',{}))
    conventions.setdefault('scrypt',config.get('difficulty_convention',{}))
    runtime=MultiRouter(AtomicState(Path(config['state_path'])),config['stratum_host'],
                       config['stratum_port'],config['control_port'],config['max_connections'],conventions)
    runner=web.AppRunner(create_multi_app(runtime,config['api_token']),access_log=None)
    stopped=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        try: loop.add_signal_handler(sig,stopped.set)
        except (NotImplementedError,RuntimeError): pass
    try:
        await runtime.start(config['stratum_host'],config['stratum_port'])
        await runner.setup()
        await web.TCPSite(runner,config['control_host'],config['control_port'],ssl_context=ssl_context).start()
        await stopped.wait()
    finally:
        await runner.cleanup();await runtime.stop()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Experimental Scrypt hash router for Debian')
    parser.add_argument('--version',action='version',version='Hash Router '+__version__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--check-config',action='store_true')
    args=parser.parse_args(argv)
    try:
        config=validate_service_config(json.loads(args.config.read_text(encoding='utf-8')))
        if args.check_config:
            tls_context(config)
            print('Configuration and TLS certificate load successfully; no listeners opened.')
        else: asyncio.run(serve(config))
    except KeyboardInterrupt: pass
    except (ValueError,OSError):
        parser.exit(2,'Cannot start hash router: invalid configuration, TLS files, or unavailable listener/state path.\n')


if __name__=='__main__': main()
