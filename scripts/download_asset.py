"""Resumable range downloader for large, SHA-256-pinned model artifacts."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
from pathlib import Path
import threading
import time
import urllib.request


def fetch(url, target, sha256, workers=6, chunk_size=8*1024*1024):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    def request(start, end):
        req = urllib.request.Request(url, headers={"Range":f"bytes={start}-{end}","User-Agent":"local-grader-init"})
        return urllib.request.urlopen(req, timeout=90)
    with request(0,0) as response:
        if response.status != 206: raise RuntimeError("Server does not support ranged downloads")
        total = int(response.headers["Content-Range"].split("/")[-1])
    partial = target.with_suffix(target.suffix+".partial")
    journal = target.with_suffix(target.suffix+".download.json")
    state = {"url":url,"sha256":sha256,"size":total,"chunk_size":chunk_size,"complete":[]}
    if journal.exists() and partial.exists():
        saved=json.loads(journal.read_text())
        if all(saved.get(k)==state[k] for k in ("url","sha256","size","chunk_size")):state=saved
        else:raise RuntimeError("Existing download is for a different revision")
    if not partial.exists():
        with partial.open("wb") as stream:stream.truncate(total)
    done=set(state["complete"]); lock=threading.Lock()
    def chunk(index):
        start=index*chunk_size;end=min(total,start+chunk_size)-1
        for attempt in range(4):
            try:
                with request(start,end) as response:
                    if response.status != 206 or response.headers["Content-Range"] != f"bytes {start}-{end}/{total}":raise RuntimeError("Range mismatch")
                    data=response.read()
                if len(data)!=end-start+1:raise RuntimeError("Incomplete range")
                with lock:
                    with partial.open("r+b") as stream:stream.seek(start);stream.write(data)
                    done.add(index);state["complete"]=sorted(done)
                    tmp=journal.with_suffix(".tmp");tmp.write_text(json.dumps(state));tmp.replace(journal)
                return
            except Exception:
                if attempt == 3:raise
                time.sleep(attempt+1)
    count=(total+chunk_size-1)//chunk_size
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(chunk,i) for i in range(count) if i not in done]
        last=time.monotonic()
        for future in as_completed(futures):
            future.result()
            if time.monotonic()-last > 15:
                print(f"{target.name}: {len(done)}/{count} chunks",flush=True);last=time.monotonic()
    with partial.open("rb") as stream: digest=hashlib.file_digest(stream,"sha256").hexdigest()
    if digest!=sha256:raise RuntimeError(f"SHA-256 mismatch: {digest}")
    partial.replace(target);journal.unlink(missing_ok=True)
    print(f"Verified: {target}",flush=True)


if __name__ == "__main__":
    p=argparse.ArgumentParser();p.add_argument("url");p.add_argument("target");p.add_argument("sha256");p.add_argument("--workers",type=int,default=6)
    args=p.parse_args();fetch(args.url,args.target,args.sha256,args.workers)
