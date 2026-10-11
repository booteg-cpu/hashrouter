"""Install the initial configuration atomically, without replacing an existing file."""
import os
import tempfile
from pathlib import Path


def copy_configuration_once(source, target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.hash-router-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(Path(source).read_bytes());stream.flush();os.fsync(stream.fileno())
        try: os.link(name,target)
        except FileExistsError: return False
        return True
    finally: os.unlink(name)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('target',type=Path)
    args=parser.parse_args()
    print('Initial configuration installed.' if copy_configuration_once(args.source,args.target) else 'Existing configuration preserved.')
