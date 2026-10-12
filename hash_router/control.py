"""Authenticated control plane; test HTTP servers are never the production launcher."""
import hmac
import ssl
import time
from aiohttp import web
from .models import RouterSnapshot,integer,text
from .metrics import DifficultyConvention


def validate_service_config(data):
    if not isinstance(data,dict): raise ValueError('Invalid service configuration')
    required=('api_token','tls_certificate','tls_key','state_path','stratum_host','stratum_port','control_host','control_port')
    if any(not data.get(key) for key in required): raise ValueError('Service configuration requires TLS and listener settings')
    token=text(data['api_token'],'api_token')
    if len(token)<32 or token.startswith('CHANGE_ME'): raise ValueError('Set a random API token of at least 32 characters')
    config=dict(data)
    for key in ('stratum_port','control_port'): config[key]=integer(data[key],key,1,65535)
    for key in ('stratum_host','control_host','tls_certificate','tls_key','state_path'): text(data[key],key)
    config['max_connections']=integer(data.get('max_connections',128),'max_connections',1,1000)
    if data.get('log_level','INFO') not in ('INFO','DEBUG'):raise ValueError('Invalid log level')
    config['log_level']=data.get('log_level','INFO')
    convention=data.get('difficulty_convention',{})
    if not isinstance(convention,dict) or type(convention.get('verified',False)) is not bool:
        raise ValueError('Calibration verified must be an explicit boolean')
    scale=DifficultyConvention(**convention)
    text(scale.name,'calibration name')
    config['difficulty_convention']={'expected_hashes_per_difficulty':str(scale.expected_hashes_per_difficulty),
                                    'verified':scale.verified,'name':scale.name}
    conventions=data.get('algorithm_difficulty_conventions',{})
    if not isinstance(conventions,dict):raise ValueError('Invalid algorithm calibrations')
    from .multi import algorithm_code
    normalized={}
    for code,convention in conventions.items():
        algorithm_code(code)
        if not isinstance(convention,dict) or type(convention.get('verified',False)) is not bool:
            raise ValueError('Calibration verified must be an explicit boolean')
        scale=DifficultyConvention(**convention);text(scale.name,'calibration name')
        normalized[code]={'expected_hashes_per_difficulty':str(scale.expected_hashes_per_difficulty),
                          'verified':scale.verified,'name':scale.name}
    config['algorithm_difficulty_conventions']=normalized
    return config


def tls_context(config):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['tls_certificate'],config['tls_key'])
    return context


def create_control_app(runtime,token):
    if not isinstance(token,str) or len(token)<32: raise ValueError('API token is too short')
    from .eventlog import EventLog
    if not hasattr(runtime,'event_log'):runtime.event_log=EventLog()
    journal=runtime.event_log;journal.protect(token)
    @web.middleware
    async def authentication(request,handler):
        started=time.monotonic()
        try:
            if request.path!='/healthz':
                actual=request.headers.get('Authorization','').encode('utf-8')
                expected=('Bearer '+token).encode('utf-8')
                if not hmac.compare_digest(actual,expected):raise web.HTTPUnauthorized(text='Authentication required')
            result=await handler(request)
        except web.HTTPException as exc:
            journal.emit('api_rejected','WARNING',method=request.method,status=exc.status,
                         duration_ms=round((time.monotonic()-started)*1000))
            raise
        except Exception as exc:
            journal.emit('api_failed','ERROR',method=request.method,error_type=type(exc).__name__)
            raise web.HTTPInternalServerError(text='Router operation failed') from None
        if request.method not in ('GET','HEAD'):
            journal.emit('api_completed',method=request.method,status=result.status,
                         duration_ms=round((time.monotonic()-started)*1000))
        return result

    async def config(request):
        try: snapshot=RouterSnapshot.from_dict(await request.json())
        except (ValueError,TypeError): raise web.HTTPBadRequest(text='Invalid routing configuration') from None
        try: await runtime.apply(snapshot)
        except ValueError: raise web.HTTPConflict(text='Configuration revision or sale obligation conflict') from None
        except OSError: raise web.HTTPServiceUnavailable(text='Cannot persist routing configuration') from None
        return web.json_response({'revision':snapshot.revision,'applied':True})

    async def status(request): return web.json_response(runtime.status().to_dict())
    async def health(request): return web.json_response({'running':runtime.running})
    async def logs(request):
        try:
            data=journal.read(after=int(request.query.get('after','0')),limit=int(request.query.get('limit','300')),
                              algorithm=request.query.get('algorithm',''),level=request.query.get('level','DEBUG'),
                              stream=request.query.get('stream',''))
        except ValueError:raise web.HTTPBadRequest(text='Invalid log query') from None
        return web.json_response(data)
    async def logging_level(request):
        try:
            data=await request.json()
            if not isinstance(data,dict) or set(data)!={'level'}:raise ValueError()
            journal.set_level(data['level'])
        except (ValueError,TypeError):raise web.HTTPBadRequest(text='Use INFO or DEBUG') from None
        return web.json_response({'level':journal.level,'temporary':True})
    app=web.Application(middlewares=[authentication],client_max_size=1024*1024)
    app.router.add_put('/v1/config',config)
    app.router.add_get('/v1/status',status)
    app.router.add_get('/healthz',health)
    app.router.add_get('/v2/logs',logs)
    app.router.add_put('/v2/logging',logging_level)
    return app
