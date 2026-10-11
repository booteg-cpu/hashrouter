"""Authenticated control plane; test HTTP servers are never the production launcher."""
import hmac
import ssl
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
    @web.middleware
    async def authentication(request,handler):
        if request.path!='/healthz':
            actual=request.headers.get('Authorization','').encode('utf-8')
            expected=('Bearer '+token).encode('utf-8')
            if not hmac.compare_digest(actual,expected): raise web.HTTPUnauthorized(text='Authentication required')
        return await handler(request)

    async def config(request):
        try: snapshot=RouterSnapshot.from_dict(await request.json())
        except (ValueError,TypeError): raise web.HTTPBadRequest(text='Invalid routing configuration') from None
        try: await runtime.apply(snapshot)
        except ValueError: raise web.HTTPConflict(text='Configuration revision or sale obligation conflict') from None
        except OSError: raise web.HTTPServiceUnavailable(text='Cannot persist routing configuration') from None
        return web.json_response({'revision':snapshot.revision,'applied':True})

    async def status(request): return web.json_response(runtime.status().to_dict())
    async def health(request): return web.json_response({'running':runtime.running})
    app=web.Application(middlewares=[authentication],client_max_size=1024*1024)
    app.router.add_put('/v1/config',config)
    app.router.add_get('/v1/status',status)
    app.router.add_get('/healthz',health)
    return app
