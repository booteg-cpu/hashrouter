"""Bounded structured diagnostics. Never accepts protocol payloads or credentials."""
import json
import logging.handlers
import queue
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

LEVELS={'DEBUG':10,'INFO':20,'WARNING':30,'ERROR':40}
FIELDS={'phase','subscribed','authorized','algorithm','connection','source_id','output_id','revision','sources','outputs',
        'port','host','reason','error_type','duration_ms','difficulty','accepted','rejected',
        'stale','target_gh','allocated_gh','shortage_gh','connections','retry_seconds','method',
        'status','count','level','nominal_gh','live_gh','reserve_gh','usable_gh','minimum_gh','extensions','mask'}


class EventLog:
    def __init__(self,path=None,capacity=2000,max_bytes=5*1024*1024,backups=3,level='INFO',keep_open=True,async_write=False):
        if level not in LEVELS:raise ValueError('Invalid diagnostic level')
        self.lock=threading.RLock();self.rows=deque(maxlen=capacity)
        self.stream=uuid.uuid4().hex;self.sequence=0;self.level=level
        self.secrets=set();self.handler=None;self.write_failed=False
        self.keep_open=keep_open
        self.pending=queue.Queue(maxsize=4096) if async_write else None
        self.stopped=threading.Event();self.writer=None;self.persist_dropped=0
        if path is not None:
            path=Path(path)
            # Recover only a bounded tail. An incomplete final line is ignored.
            try:
                path.parent.mkdir(parents=True,exist_ok=True)
                if path.exists():
                    with path.open('rb') as source:
                        source.seek(max(0,path.stat().st_size-1024*1024))
                        for line in source.read().splitlines():
                            try:
                                row=json.loads(line)
                                if not isinstance(row,dict) or not isinstance(row.get('seq'),int):continue
                                if not isinstance(row.get('stream'),str):continue
                                if row['seq']>self.sequence:
                                    self.sequence=row['seq'];self.stream=row['stream'];self.rows.append(row)
                            except (ValueError,UnicodeError):pass
                self.handler=logging.handlers.RotatingFileHandler(path,maxBytes=max_bytes,backupCount=backups,encoding='utf-8',delay=not keep_open)
                if path.exists() and path.stat().st_size:
                    with path.open('rb') as source:
                        source.seek(-1,2);unfinished=source.read(1)!=b'\n'
                    if unfinished:
                        with path.open('a',encoding='utf-8') as target:target.write('\n')
            except OSError:self.write_failed=True
        if self.pending is not None and self.handler is not None:
            self.writer=threading.Thread(target=self._writer,daemon=True,name='router-event-writer');self.writer.start()

    def _writer(self):
        while not self.stopped.is_set() or not self.pending.empty():
            try:row=self.pending.get(timeout=.1)
            except queue.Empty:continue
            try:self._persist(row)
            finally:self.pending.task_done()

    def _persist(self,row):
        try:
            line=json.dumps(row,ensure_ascii=True,allow_nan=False)
            record=logging.LogRecord('hash_router',LEVELS[row['level']],'',0,line,(),None)
            if self.handler.shouldRollover(record):self.handler.doRollover()
            # Avoid logging.handleError printing a record during disk failures.
            if self.handler.stream is None:self.handler.stream=self.handler._open()
            self.handler.stream.write(line+'\n');self.handler.flush()
        except (OSError,ValueError):self.write_failed=True
        finally:
            if not self.keep_open:self.handler.close()

    def protect(self,*values):
        with self.lock:
            self.secrets.update(v for v in values if isinstance(v,str) and v)

    def _text(self,value):
        value=str(value)
        for secret in sorted(self.secrets,key=len,reverse=True):
            if len(secret)>=8:value=value.replace(secret,'[redacted]')
            elif value==secret:value='[redacted]'
        return ''.join(c if c.isprintable() else ' ' for c in value)[:160]

    def emit(self,event,level='INFO',**fields):
        with self.lock:
            if LEVELS[level]<LEVELS[self.level]:return
            self.sequence+=1
            row={'stream':self.stream,'seq':self.sequence,
                 'utc':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                 'level':level,'event':self._text(event)}
            for key,value in fields.items():
                if key in FIELDS and value is not None:
                    row[key]=value if type(value) in (int,bool) else self._text(value)
            self.rows.append(row)
            if self.handler is not None:
                if self.pending is None:self._persist(row)
                elif not self.stopped.is_set():
                    try:self.pending.put_nowait(row)
                    except queue.Full:self.persist_dropped+=1

    def read(self,after=0,limit=300,algorithm='',level='DEBUG',stream=''):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=500 or level not in LEVELS:
            raise ValueError('Invalid log cursor, limit or level')
        with self.lock:
            reset=bool(stream and stream!=self.stream) or after>self.sequence
            if reset:after=0
            oldest=self.rows[0]['seq'] if self.rows else self.sequence+1
            gap=after>0 and after<oldest-1
            result=[];cursor=after
            for row in self.rows:
                if row['seq']<=after:continue
                cursor=row['seq']
                if (not algorithm or row.get('algorithm')==algorithm) and LEVELS.get(row.get('level'),0)>=LEVELS[level]:
                    result.append(dict(row))
                    if len(result)>=limit:break
            return {'stream':self.stream,'entries':result,'next':cursor,'oldest':oldest,
                    'latest':self.sequence,'gap':gap,'reset':reset,'level':self.level,
                    'persistent':self.handler is not None,'write_failed':self.write_failed,'persist_dropped':self.persist_dropped}

    def set_level(self,level):
        if level not in ('INFO','DEBUG'):raise ValueError('Use INFO or DEBUG')
        with self.lock:
            self.level=level;self.emit('logging_level_changed',level=level)

    def close(self):
        self.stopped.set()
        if self.writer:
            self.writer.join(timeout=2)
            if self.writer.is_alive():return
        with self.lock:
            if self.handler:self.handler.close();self.handler=None
