"""Validated, immutable control-plane values. Hashrate uses GH/s throughout."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal, InvalidOperation
import math
from datetime import datetime, timezone


def utc_time(value):
    try: return float(number(value,'UTC timestamp'))
    except ValueError:
        value=str(value).strip()
        if value.endswith(' UTC'): value=value[:-4]+'+00:00'
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None: date=date.replace(tzinfo=timezone.utc)
        return date.timestamp()


def number(value, name='value', *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Invalid {name}') from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f'Invalid {name}')
    return result


def integer(value, name, minimum=0, maximum=2**63-1):
    if isinstance(value, bool):
        raise ValueError(f'Invalid {name}')
    try:
        result = int(value)
        if Decimal(str(value)) != result or not minimum <= result <= maximum:
            raise ValueError
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        raise ValueError(f'Invalid {name}') from None
    return result


def text(value, name):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError(f'Invalid {name}')
    return value


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
    username: str
    password: str = field(repr=False)

    def __post_init__(self):
        text(self.host, 'host')
        if any(c in self.host for c in '/@ \\'):
            raise ValueError('Host must be a hostname or IP, without a scheme')
        integer(self.port, 'port', 1, 65535)
        text(self.username, 'username')
        text(self.password, 'password')

    @classmethod
    def from_dict(cls, data):
        return cls(data['host'], integer(data['port'], 'port', 1, 65535), data['username'], data['password'])


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str
    account_id: str
    rig_id: str
    rental_id: str
    advertised_gh: Decimal
    expires_at: float
    username: str
    password: str = field(repr=False)

    def __post_init__(self):
        for name in ('source_id', 'account_id', 'rig_id', 'rental_id', 'username', 'password'):
            text(getattr(self, name), name)
        object.__setattr__(self, 'advertised_gh', number(self.advertised_gh, 'advertised_gh', positive=True))
        expires = float(number(self.expires_at, 'expires_at', positive=True))
        if not math.isfinite(expires): raise ValueError('Invalid expires_at')
        object.__setattr__(self, 'expires_at', expires)

    @classmethod
    def from_dict(cls, data):
        if data.get('algorithm', 'scrypt').lower() != 'scrypt' or data.get('source_mode', 'mrr') != 'mrr':
            raise ValueError('Only Scrypt sources from MRR are supported')
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class OutputSpec:
    output_id: str
    mrr_rig_id: str | None
    target_gh: Decimal
    endpoint: Endpoint | None
    rented_until: float | None = None
    idle_hash_gh: Decimal = Decimal('0')
    rental_id: str | None = None

    def __post_init__(self):
        text(self.output_id, 'output_id')
        if self.output_id == 'remainder': raise ValueError('Reserved output ID')
        if self.mrr_rig_id is not None: text(self.mrr_rig_id, 'mrr_rig_id')
        if self.rental_id is not None: text(self.rental_id, 'output rental_id')
        object.__setattr__(self, 'target_gh', number(self.target_gh, 'target_gh', positive=True))
        object.__setattr__(self, 'idle_hash_gh', number(self.idle_hash_gh, 'idle_hash_gh'))
        if self.idle_hash_gh > self.target_gh: raise ValueError('Idle hash exceeds target')
        if self.rented_until is not None:
            value = float(number(self.rented_until, 'rented_until', positive=True))
            if not math.isfinite(value): raise ValueError('Invalid rented_until')
            object.__setattr__(self, 'rented_until', value)

    @classmethod
    def from_dict(cls, data):
        return cls(data['output_id'], data.get('mrr_rig_id'), data['target_gh'],
                   Endpoint.from_dict(data['endpoint']) if data.get('endpoint') else None,
                   data.get('rented_until'), data.get('idle_hash_gh', '0'), data.get('rental_id'))


@dataclass(frozen=True, slots=True)
class PricePolicy:
    mode: str
    manual_btc_per_gh_day: Decimal | None
    modifier_pct: Decimal = Decimal('0')


@dataclass(frozen=True, slots=True)
class RouterSnapshot:
    schema_version: int
    revision: int
    sources: tuple[SourceSpec, ...]
    outputs: tuple[OutputSpec, ...]
    remainder: Endpoint
    reserve_seconds: float = 120
    quantum_seconds: float = 1
    algorithm: str = 'scrypt'

    def __post_init__(self):
        import re
        if not isinstance(self.algorithm,str) or not re.fullmatch(r'[a-z0-9_-]{1,64}',self.algorithm):
            raise ValueError('Invalid algorithm code')
        if integer(self.schema_version, 'schema_version') != 1: raise ValueError('Unsupported schema version')
        integer(self.revision, 'revision')
        object.__setattr__(self, 'sources', tuple(self.sources))
        object.__setattr__(self, 'outputs', tuple(self.outputs))
        for sequence, name in ((self.sources, 'source_id'), (self.outputs, 'output_id')):
            ids = [getattr(item, name) for item in sequence]
            if len(ids) != len(set(ids)) or len(ids) > 1000: raise ValueError(f'Invalid {name} list')
        logins = [s.username for s in self.sources]
        if len(logins) != len(set(logins)): raise ValueError('Duplicate input username')
        for name in ('reserve_seconds', 'quantum_seconds'):
            value = float(number(getattr(self, name), name, positive=True))
            if not math.isfinite(value): raise ValueError(f'Invalid {name}')
            object.__setattr__(self, name, value)

    @classmethod
    def from_dict(cls, data):
        try:
            return cls(integer(data['schema_version'], 'schema_version'), integer(data['revision'], 'revision'),
                       tuple(SourceSpec.from_dict(s) for s in data['sources']),
                       tuple(OutputSpec.from_dict(o) for o in data['outputs']),
                       Endpoint.from_dict(data['remainder']), data.get('reserve_seconds', 120), data.get('quantum_seconds', 1), data.get('algorithm','scrypt'))
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Incomplete routing configuration') from None

    def to_dict(self, include_secrets=False):
        def convert(value):
            if isinstance(value, Decimal): return format(value, 'f')
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items() if include_secrets or k != 'password'}
            if isinstance(value, (tuple, list)): return [convert(v) for v in value]
            return value
        return convert(asdict(self))
