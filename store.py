import sqlite3,json,time,uuid,hashlib,threading
from pathlib import Path

def uid():return uuid.uuid4().hex

def dump(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def digest(x):return hashlib.sha256(dump(x).encode()).hexdigest()

class Store:
 def __init__(self,path):
  self.root=Path(path).resolve();self.root.mkdir(parents=True,exist_ok=True);self.projects=self.root/'projects';self.projects.mkdir(exist_ok=True);self.lock=threading.RLock()
  with self.db() as c:c.executescript("""CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,workspace TEXT,body TEXT,updated REAL,PRIMARY KEY(kind,id));CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run TEXT,type TEXT,body TEXT,created REAL);CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY,workspace TEXT,path TEXT,before BLOB,after BLOB,created REAL,reason TEXT);CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY,workspace TEXT,model TEXT,hash TEXT,vector TEXT);CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT,target TEXT,body TEXT,created REAL);""")
  with self.db() as c:
   if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='objects'").fetchone():
    c.execute('INSERT OR IGNORE INTO records(kind,id,workspace,body,updated) SELECT kind,id,workspace,body,updated FROM objects')
 def db(self):
  c=sqlite3.connect(self.root/'application.sqlite',timeout=30);c.row_factory=sqlite3.Row;return c
 def put(self,kind,id,body,workspace=None):
  body={**body,'id':id}
  with self.db() as c:c.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)',(kind,id,workspace,dump(body),time.time()))
  return body
 def get(self,kind,id,required=True):
  with self.db() as c:r=c.execute('SELECT body FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
  if not r:
   if required:raise ValueError(kind+' not found')
   return None
  return json.loads(r['body'])
 def all(self,kind,workspace=None):
  with self.db() as c:rows=c.execute('SELECT body FROM records WHERE kind=?'+(' AND workspace=?' if workspace is not None else '')+' ORDER BY updated DESC',(kind,workspace) if workspace is not None else (kind,)).fetchall()
  return [json.loads(r['body']) for r in rows]
 def delete(self,kind,id):
  with self.db() as c:
   c.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,id))
   if kind=='memory':c.execute('DELETE FROM vectors WHERE id=?',(id,))
 def log(self,action,target,body=None):
  with self.db() as c:c.execute('INSERT INTO audit(action,target,body,created) VALUES(?,?,?,?)',(action,str(target),dump(body or {}),time.time()))
 def event(self,id,type,body):
  with self.db() as c:cur=c.execute('INSERT INTO events(run,type,body,created) VALUES(?,?,?,?)',(id,type,dump(body),time.time()));return cur.lastrowid
 def events(self,id,after=0):
  with self.db() as c:rows=c.execute('SELECT * FROM events WHERE run=? AND seq>? ORDER BY seq',(id,after)).fetchall()
  return [{**dict(r),'body':json.loads(r['body'])} for r in rows]
 def project(self,id):
  self.get('workspace',id);p=(self.projects/id).resolve()
  if p.parent!=self.projects:raise ValueError('Invalid workspace')
  p.mkdir(exist_ok=True);return p
 def path(self,id,rel,root_ok=False):
  root=self.project(id)
  if rel=='' and root_ok:return root
  if not isinstance(rel,str) or not rel or rel.startswith('/') or chr(92) in rel or ':' in rel or any(x in ('.','..','') for x in rel.split('/')):raise ValueError('Invalid project-relative path')
  p=root
  for part in rel.split('/'):
   p=p/part
   if p.is_symlink() or getattr(p,'is_junction',lambda:False)():raise ValueError('Symlinks/junctions denied')
  p=p.resolve()
  if p==root or not p.is_relative_to(root):raise ValueError('Outside workspace')
  return p
 def write(self,id,rel,value,reason='write',expected=None):
  with self.lock:
   p=self.path(id,rel)
   if p.exists() and (not p.is_file() or p.stat().st_size>2000000):raise ValueError('Invalid/oversized file')
   before=p.read_bytes() if p.exists() else None;sha=hashlib.sha256(before).hexdigest() if before is not None else 'missing'
   if expected is not None and sha!=expected:raise ValueError('File changed since review')
   if value is not None and len(value)>2000000:raise ValueError('File too large')
   cp=uid()
   with self.db() as c:c.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)',(cp,id,rel,before,value,time.time(),reason))
   if value is None:
    if p.exists():p.unlink()
   else:
    p.parent.mkdir(parents=True,exist_ok=True);temp=p.parent/('.agent-'+uid())
    try:temp.write_bytes(value);temp.replace(p)
    finally:
     if temp.exists():temp.unlink()
   self.log('file.'+reason,rel,{'workspace':id,'checkpoint':cp});return {'path':rel,'checkpoint':cp}
 def checkpoint(self,workspace,id):
  with self.db() as c:r=c.execute('SELECT * FROM checkpoints WHERE id=? AND workspace=?',(id,workspace)).fetchone()
  if not r:raise ValueError('Checkpoint not found')
  return dict(r)
