"""Atomic private JSON storage. Invalid state is an error, never an empty inventory."""
import json
import os
import tempfile
import threading
from pathlib import Path


class AtomicState:
    def __init__(self, path: Path):
        self.path=Path(path)
        self.lock=threading.RLock()

    def load(self):
        with self.lock:
            try:
                with self.path.open(encoding='utf-8') as stream:
                    value=json.load(stream)
            except FileNotFoundError:
                return {}
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError('Invalid saved router state') from None
            if not isinstance(value,dict): raise ValueError('Invalid saved router state')
            return value

    def save(self, value):
        if not isinstance(value,dict): raise ValueError('State must be an object')
        payload=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(prefix=self.path.name+'.',suffix='.tmp',dir=self.path.parent)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as stream:
                    stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                os.replace(name,self.path)
                if os.name=='posix':
                    directory=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try: os.fsync(directory)
                    finally: os.close(directory)
            finally:
                try: os.unlink(name)
                except FileNotFoundError: pass
