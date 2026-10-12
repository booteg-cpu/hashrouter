"""Initial Debian connection setup; reruns preserve existing credentials/state."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import ssl
import sys
import subprocess
import tempfile
from pathlib import Path


def connection_defaults(path='/etc/hash-router/config.json',env=None):
    """Read existing public settings or inspect local routes; no external probes."""
    config=json.loads(Path(path).read_text()) if Path(path).exists() else {}
    if not isinstance(config,dict):raise ValueError('Invalid saved configuration')
    saved=config.get('public_host')
    port=config.get('control_port',8443)
    if type(port) is not int or not 1<=port<=65535 or 3333<=port<=3433:
        raise ValueError('Invalid saved API port')
    if saved:
        if not isinstance(saved,str):raise ValueError('Invalid saved public host')
        return host_name(saved),port
    environment=os.environ if env is None else env
    ssh=environment.get('SSH_CONNECTION','').split()
    candidates=[ssh[2]] if len(ssh)==4 else []
    try:
        result=subprocess.run(['ip','-j','route','get','1.1.1.1'],capture_output=True,text=True,check=True,timeout=3)
        candidates.extend(r.get('prefsrc','') for r in json.loads(result.stdout))
    except (OSError,ValueError,subprocess.SubprocessError,AttributeError,TypeError):pass
    for value in candidates:
        try:
            address=ipaddress.ip_address(value)
            if address.is_global:return str(address),port
        except ValueError:pass
    try:
        result=subprocess.run(['ip','-j','address','show','scope','global'],capture_output=True,text=True,check=True,timeout=3)
        addresses={str(ipaddress.ip_address(a['local'])) for row in json.loads(result.stdout)
                   for a in row.get('addr_info',[]) if ipaddress.ip_address(a['local']).is_global}
        if len(addresses)==1:return addresses.pop(),port
    except (OSError,ValueError,KeyError,subprocess.SubprocessError,AttributeError,TypeError):pass
    return '',port


def host_name(value):
    value=value.strip()
    try:return str(ipaddress.ip_address(value))
    except ValueError:pass
    if len(value)>253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',value):
        raise ValueError('Specify a public IP address or DNS name, without scheme or port')
    if any(not part or len(part)>63 or part.startswith('-') or part.endswith('-') for part in value.split('.')):
        raise ValueError('Invalid DNS name')
    return value.lower()


def write_json(path,value):
    path=Path(path);fd,name=tempfile.mkstemp(dir=path.parent,prefix='.connection-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=True,indent=2);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def certificate_fingerprint(path):
    """SHA-256 of the leaf certificate DER, as seen in the TLS handshake."""
    der=ssl.PEM_cert_to_DER_cert(Path(path).read_text(encoding='ascii'))
    digest=hashlib.sha256(der).hexdigest().upper()
    return ':'.join(digest[index:index+2] for index in range(0,len(digest),2))


def initialize(directory,state_dir,public_host,port=8443,openssl='openssl',*,export_connection=False):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    public_host=host_name(public_host)
    if type(port) is not int or not 1<=port<=65535 or 3333<=port<=3433:
        raise ValueError('API port must be outside reserved Stratum range 3333..3433')
    path=directory/'config.json';config=json.loads(path.read_text()) if path.exists() else {}
    fresh=not config or str(config.get('api_token','')).startswith('CHANGE_ME')
    if fresh:
        ca_key=directory/'connection-ca.key';ca=directory/'connection-ca.pem'
        key=directory/'privkey.pem';cert=directory/'fullchain.pem'
        # Recover an interrupted initial setup without rotating an existing CA.
        if ca.exists()!=ca_key.exists():raise ValueError('Incomplete CA files; recover setup before retrying')
        def run(*args):
            subprocess.run([openssl,*map(str,args)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        if not ca.exists():
            run('req','-x509','-newkey','rsa:3072','-nodes','-days','3650','-sha256',
                '-subj','/CN=Hashroute local connection CA','-keyout',ca_key,'-out',ca,
                '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
                '-addext','subjectKeyIdentifier=hash','-addext','authorityKeyIdentifier=keyid:always')
        csr=directory/'server.csr';extensions=directory/'server.ext'
        try:
            try:ipaddress.ip_address(public_host);san='IP:'+public_host
            except ValueError:san='DNS:'+public_host
            extensions.write_text('subjectAltName='+san+'\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n',encoding='ascii')
            run('req','-new','-newkey','rsa:3072','-nodes','-subj','/CN=Hashroute',
                '-keyout',key,'-out',csr)
            run('x509','-req','-in',csr,'-CA',ca,'-CAkey',ca_key,'-CAcreateserial','-days','365',
                '-sha256','-extfile',extensions,'-out',cert)
        finally:
            csr.unlink(missing_ok=True);extensions.unlink(missing_ok=True)
        for secret in (ca_key,key):secret.chmod(0o600)
        config={'api_token':secrets.token_urlsafe(48),'tls_certificate':str(cert),'tls_key':str(key),
                'state_path':str(Path(state_dir)/'state.json'),'stratum_host':'0.0.0.0','stratum_port':3333,
                'control_host':'0.0.0.0','control_port':port,'max_connections':128,
                'public_host':public_host,'connection_ca_path':str(ca)}
        write_json(path,config)
    if len(config.get('api_token',''))<32:raise ValueError('Invalid existing API token')
    public_host=host_name(config.get('public_host') or public_host)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config['tls_certificate'],config['tls_key'])
    ca_path=config.get('connection_ca_path')
    public_ca=Path(ca_path).read_text(encoding='ascii') if ca_path else ''
    authority='['+public_host+']' if ':' in public_host else public_host
    bundle={'format':'hashroute-connection-v1','url':f'https://{authority}:{int(config["control_port"])}',
            'token':config['api_token'],'public_host':public_host,'tls_ca_pem':public_ca}
    if export_connection:
        write_json(directory/'sbp-connection.json',bundle)
    return bundle,config


def show_connection(bundle,config,fingerprint,*,export_connection=False):
    color=sys.stdout.isatty() and 'NO_COLOR' not in os.environ
    def emphasized(value,code='1;36'):
        return '\033['+code+'m'+value+'\033[0m' if color else value
    print('\n'+'='*64)
    print(emphasized('HASH ROUTER - CONNECTION SETTINGS','1;32'))
    print('='*64)
    print('VPS IP / DNS: '+emphasized(bundle['public_host']))
    print('HTTPS API port: '+emphasized(str(config['control_port'])))
    print('SBP connection address: '+bundle['url'])
    print('\nSBP access key (copy the next line):')
    print(emphasized(bundle['token']))
    print('\nTLS certificate SHA-256 (two lines):')
    print(fingerprint[:47])
    print(fingerprint[48:])
    print('Compare this fingerprint over trusted SSH before trusting the server in SBP.')
    print('Do not send the access key before the certificate has been verified.')
    if export_connection:
        print('Optional import file: /etc/hash-router/sbp-connection.json (contains the access key).')
    print('File-free connection requires certificate-pin support in SBP.')
    print('HTTPS certificate expires after 365 days; renew using the existing CA.')
    print('='*64+'\n')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--host')
    parser.add_argument('--defaults',action='store_true',help='Print public host and API port defaults only')
    parser.add_argument('--port',type=int,default=8443)
    parser.add_argument('--export-connection',action='store_true',
                        help='Also write the optional legacy SBP import file')
    parser.add_argument('--quiet',action='store_true',help='Do not print connection credentials')
    args=parser.parse_args()
    if args.defaults:
        try:host,port=connection_defaults()
        except (ValueError,OSError):parser.exit(2,'Cannot read saved connection settings.\n')
        print(host);print(port);return
    if not args.host:parser.error('--host is required unless --defaults is used')
    try:
        bundle,config=initialize('/etc/hash-router','/var/lib/hash-router',args.host,args.port,
                                 export_connection=args.export_connection)
        fingerprint=certificate_fingerprint(config['tls_certificate'])
    except (ValueError,OSError,KeyError,subprocess.CalledProcessError):
        parser.exit(2,'Connection setup failed; existing credentials/state were preserved. Check TLS files and public address.\n')
    if not args.quiet:
        show_connection(bundle,config,fingerprint,export_connection=args.export_connection)


if __name__=='__main__':main()
