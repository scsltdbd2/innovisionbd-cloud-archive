"""InnovisionBD V12 Google Cloud Run archive worker.

Cloud worker never opens MariaDB. It leases files from the protected HTTPS PHP
backend, downloads source Google Drive file ranges and uploads via Google's
resumable protocol to the authorized Workspace Drive. One file's session URI is
checkpointed in MySQL through the gateway. Every change is verified on Drive.
"""
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("ivbd_archive")

GATEWAY = os.getenv("IVBD_GATEWAY_URL", "https://innovisionbd.com/business-data/cloud_gateway.php")
SECRET = os.getenv("IVBD_GATEWAY_SECRET", "")
RUN_SECRET = os.getenv("IVBD_RUN_SECRET", "")
WORKERS = min(16, max(1, int(os.getenv("IVBD_WORKERS", "6"))))
RUN_SECONDS = min(120, max(15, int(os.getenv("IVBD_RUN_SECONDS", "45"))))
FILE_SLICE_SECONDS = min(195, max(30, int(os.getenv("IVBD_FILE_SLICE_SECONDS", "165"))))
CHUNK_BYTES = 8 * 1024 * 1024  # 256 KiB-aligned Google resumable chunks.
GOOGLE = "https://www.googleapis.com"
SESSION = threading.local()

NATIVE = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


class ArchiveError(Exception):
    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


def http_session():
    if not hasattr(SESSION, "client"):
        client = requests.Session()
        client.headers["User-Agent"] = "InnovisionBD-CloudArchive/12.0"
        SESSION.client = client
    return SESSION.client


def gateway(action, **kwargs):
    if not SECRET or len(SECRET) != 64:
        raise ArchiveError("IVBD_GATEWAY_SECRET is missing or invalid", retry=False)
    try:
        response = http_session().post(
            GATEWAY, headers={"X-Archive-Secret": SECRET},
            json={"action": action, **kwargs}, timeout=(12, 110),
        )
    except requests.RequestException as exc:
        raise ArchiveError("Hosting gateway unreachable: " + type(exc).__name__) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise ArchiveError("Hosting gateway returned non-JSON response (HTTP %d)" % response.status_code) from exc
    if not response.ok or not body.get("ok"):
        raise ArchiveError("Hosting gateway HTTP %s: %s" % (response.status_code, body.get("error", "failed")))
    return body


def google_call(method, url, token, *, params=None, payload=None, data=None, headers=None, stream=False, timeout=95):
    h={"Authorization": "Bearer " + token}
    if headers: h.update(headers)
    if payload is not None:
        h["Content-Type"]="application/json; charset=UTF-8"
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(5):
        try:
            r=http_session().request(method, url, headers=h, params=params, data=data,
                                     stream=stream, timeout=(12,timeout), allow_redirects=False)
        except requests.RequestException as exc:
            if attempt==4: raise ArchiveError("Google network error: "+type(exc).__name__) from exc
            time.sleep(min(9, 2**attempt + random.random()));continue
        if r.status_code in (429,500,502,503,504) or (r.status_code==403 and
           ("rateLimitExceeded" in r.text[:500] or "userRateLimitExceeded" in r.text[:500])):
            r.close()
            if attempt==4:raise ArchiveError("Google API throttled after retries")
            time.sleep(min(9,2**attempt+random.random()));continue
        return r
    raise ArchiveError("Google API unavailable")


def json_checked(response, allowed=(200,201)):
    if response.status_code not in allowed:
        status=response.status_code
        body=response.text[:180].replace("\n"," ")
        response.close()
        raise ArchiveError("Google API HTTP %s: %s" % (status, body), retry=status in (408,409,429,500,502,503,504))
    try: j=response.json()
    except ValueError as exc:
        response.close();raise ArchiveError("Invalid Google API JSON response") from exc
    response.close()
    return j


def details(work):
    r=google_call("GET",GOOGLE+"/drive/v3/files/"+quote(work["file_id"],safe=""),
                  work["source_token"],params={"fields":"id,name,mimeType,size,md5Checksum,modifiedTime,shortcutDetails,capabilities(canDownload)","supportsAllDrives":"true"})
    d=json_checked(r)
    if d.get("capabilities",{}).get("canDownload") is False:
        raise ArchiveError("Source file does not permit download",retry=False)
    if work.get("revision") and d.get("modifiedTime") and work["revision"]!=d["modifiedTime"]:
        raise ArchiveError("Source modified during archive; run another Drive scan",retry=False)
    return d


def file_mime_name(work,meta):
    original_mime=meta.get("mimeType") or work["mime"]
    native=original_mime in NATIVE
    if native:
        mime,ext=NATIVE[original_mime]
        name=work["name"] if work["name"].lower().endswith(ext) else work["name"]+ext
        return mime,name,True
    if original_mime.startswith("application/vnd.google-apps."):
        return "application/json",work["name"]+".archive-manifest.json",False
    return original_mime or "application/octet-stream",work["name"],False


def export_bytes(work,mime):
    r=google_call("GET",GOOGLE+"/drive/v3/files/"+quote(work["file_id"],safe="")+"/export",
                   work["source_token"],params={"mimeType":mime},timeout=130,stream=True)
    if r.status_code!=200:
        code=r.status_code;body=r.text[:160];r.close()
        raise ArchiveError("Google export HTTP %s: %s"%(code,body),retry=code in (429,500,502,503,504))
    pieces=[];size=0
    try:
        for part in r.iter_content(1024*1024):
            if not part: continue
            size+=len(part)
            if size>24*1024*1024:
                raise ArchiveError("Native file export exceeds supported Google export size; original cannot be fully archived",retry=False)
            pieces.append(part)
    finally:r.close()
    return b"".join(pieces)


def source_range(work,start,end,total):
    if not (0 <= start <= end < total):raise ArchiveError("Invalid source byte range",retry=False)
    headers={"Range":"bytes=%d-%d"%(start,end)}
    r=google_call("GET",GOOGLE+"/drive/v3/files/"+quote(work["file_id"],safe=""),
                  work["source_token"],params={"alt":"media","supportsAllDrives":"true"},
                  headers=headers,stream=True,timeout=150)
    if r.status_code not in (200,206):
        code=r.status_code;body=r.text[:140];r.close()
        raise ArchiveError("Source download HTTP %s: %s"%(code,body),retry=code in (429,500,502,503,504))
    if r.status_code==200 and start!=0:
        r.close();raise ArchiveError("Source does not support byte ranges; large file cannot be safely resumed",retry=False)
    if r.status_code==206:
        content_range=r.headers.get("Content-Range","")
        if not content_range.startswith("bytes %d-"%start):
            r.close();raise ArchiveError("Source server returned unexpected byte range")
    want=end-start+1
    buf=bytearray()
    try:
        for part in r.iter_content(1024*1024):
            if part:
                buf.extend(part)
                if len(buf)>want:break
    finally:r.close()
    if len(buf)!=want:
        raise ArchiveError("Incomplete source range: expected %d, received %d bytes"%(want,len(buf)))
    return bytes(buf)


def search_existing(work,meta,expected_size):
    # App properties are private to the OAuth app. This repairs a cloud worker
    # crash after upload succeeded but before MySQL 'complete' was committed.
    key=work["source_key"]
    query="trashed = false and appProperties has { key='ivbd_source' and value='%s' }"%key
    r=google_call("GET",GOOGLE+"/drive/v3/files",work["destination_token"],params={
      "q":query,"spaces":"drive","fields":"nextPageToken,files(id,name,size,mimeType,md5Checksum,appProperties,parents)","pageSize":100})
    records=json_checked(r).get("files",[])
    expected_rev=hashlib.sha256(work["revision"].encode()).hexdigest()
    for item in records:
        if item.get("appProperties",{}).get("ivbd_rev")==expected_rev and str(item.get("size",""))==str(expected_size):
            if meta.get("md5Checksum") and item.get("md5Checksum") != meta["md5Checksum"]:
                continue
            return item,True
    return records[0] if records else None,False


def start_session(work,name,mime,size,existing=None):
    meta={"name":name,"mimeType":mime,
          "appProperties":{"ivbd_source":work["source_key"],"ivbd_rev":hashlib.sha256(work["revision"].encode()).hexdigest()}}
    base=GOOGLE+"/upload/drive/v3/files"
    if existing:
        url=base+"/"+quote(existing,safe="")
        method="PATCH"
    else:
        meta["parents"]=[work["folder_id"]]
        url=base;method="POST"
    r=google_call(method,url,work["destination_token"],params={"uploadType":"resumable","fields":"id,name,size,mimeType,md5Checksum,parents,appProperties"},
                  payload=meta,headers={"X-Upload-Content-Type":mime,"X-Upload-Content-Length":str(size)},timeout=120)
    if r.status_code not in (200,201):
        code=r.status_code;body=r.text[:150];r.close()
        raise ArchiveError("Upload initialization HTTP %s: %s"%(code,body),retry=code in (429,500,502,503,504))
    uri=r.headers.get("Location","")
    r.close()
    if not uri.startswith("https://www.googleapis.com/upload/drive/"):
        raise ArchiveError("Google did not return a valid resumable upload URL")
    gateway("progress",task_id=work["task_id"],lease=work["lease"],session_url=uri,bytes=0)
    return uri


def session_status(work,uri,size):
    headers={"Content-Range":"bytes */%d"%size,"Content-Length":"0"}
    r=google_call("PUT",uri,work["destination_token"],headers=headers,data=b"",timeout=75)
    if r.status_code in (200,201):return (size,json_checked(r))
    if r.status_code==404:
        r.close();return None,None
    if r.status_code!=308:
        code=r.status_code;body=r.text[:150];r.close()
        raise ArchiveError("Google resumable session query HTTP %s: %s"%(code,body))
    received=r.headers.get("Range","")
    r.close()
    m=re.fullmatch(r"bytes=0-(\d+)",received)
    return (int(m.group(1))+1 if m else 0),None


def save_chunk(work,uri,offset,chunk,size):
    end=offset+len(chunk)-1
    headers={"Content-Range":"bytes %d-%d/%d"%(offset,end,size),
             "Content-Length":str(len(chunk)),"Content-Type":"application/octet-stream"}
    r=google_call("PUT",uri,work["destination_token"],headers=headers,data=chunk,timeout=180)
    if r.status_code in (200,201):return size,json_checked(r)
    if r.status_code==308:
        ran=r.headers.get("Range","")
        r.close()
        m=re.fullmatch(r"bytes=0-(\d+)",ran)
        next_pos=int(m.group(1))+1 if m else 0
        if next_pos<=offset and len(chunk)>0:raise ArchiveError("Google upload did not accept this chunk")
        return next_pos,None
    code=r.status_code;body=r.text[:140];r.close()
    if code==404:raise ArchiveError("Resumable upload session expired")
    raise ArchiveError("Google resumable upload HTTP %s: %s"%(code,body),retry=code in (408,409,429,500,502,503,504))


def metadata_empty(work,name,mime):
    payload={"name":name,"mimeType":mime,"parents":[work["folder_id"]],
             "appProperties":{"ivbd_source":work["source_key"],"ivbd_rev":hashlib.sha256(work["revision"].encode()).hexdigest()}}
    r=google_call("POST",GOOGLE+"/drive/v3/files",work["destination_token"],params={"fields":"id,name,size,mimeType"},payload=payload)
    return json_checked(r)


def verify(work,created,total,src_md5):
    dest=created.get("id")
    if not dest:raise ArchiveError("Google returned no destination file ID")
    r=google_call("GET",GOOGLE+"/drive/v3/files/"+quote(dest,safe=""),work["destination_token"],
                   params={"fields":"id,name,mimeType,size,md5Checksum,parents,appProperties,trashed"})
    record=json_checked(r)
    if record.get("trashed") or int(record.get("size",-1))!=int(total):
        raise ArchiveError("Destination size mismatch / file deleted during verification")
    if src_md5 and record.get("md5Checksum") and record["md5Checksum"]!=src_md5:
        raise ArchiveError("Destination checksum differs from source")
    if record.get("appProperties",{}).get("ivbd_source")!=work["source_key"]:
        # Pre-V12 destination files do not have private appProperties;
        # only fail on newly created records where this indicates a bad copy.
        if not work.get("destination_id"):
            raise ArchiveError("Destination source marker missing")
    return record


def archive_one(work):
    started=time.monotonic()
    meta=details(work)
    mime,name,native=file_mime_name(work,meta)
    representation=meta.get("mimeType",work["mime"]).startswith("application/vnd.google-apps.") and not native
    fidelity="manifest" if representation else ("export" if native else "original")
    if representation:
        body=json.dumps({"warning":"Metadata manifest only. The native Google file content was NOT backed up.",
                         "source_file_id":work["file_id"],"source_mime":meta.get("mimeType"),
                         "source_name":work["name"],"modified_time":meta.get("modifiedTime"),
                         "shortcut_details":meta.get("shortcutDetails")},ensure_ascii=False,indent=2).encode("utf-8")
    else:
        body=export_bytes(work,mime) if native else None
    if body is not None:
        size=len(body)
    else:
        reported_size=meta.get("size") if meta.get("size") is not None else work.get("size")
        if reported_size is None:
            raise ArchiveError("Original file size unavailable; refusing to archive an empty substitute",retry=False)
        size=int(reported_size)
    src_md5=meta.get("md5Checksum") if fidelity=="original" else None
    existing,already=search_existing(work,meta,size)
    if already:
        result=verify(work,existing,size,src_md5)
        gateway("complete",task_id=work["task_id"],lease=work["lease"],
                destination_id=result["id"],destination_name=name,mime=mime,bytes=size,fidelity=fidelity)
        return "already"
    # Existing V9 copies have a DB ID but no V12 appProperties.
    existing_id=work.get("destination_id") or (existing or {}).get("id")
    if size==0:
        if existing_id:
            # Existing old file can be overwritten by a zero-byte resumable session.
            existing_id=None  # Creating a new empty app-owned record is more reliable.
        created=metadata_empty(work,name,mime)
    else:
        uri=work.get("session_url")
        result=None
        if uri:
            position,result=session_status(work,uri,size)
            if position is None:
                uri=None
        else:position=0
        if not uri:
            try:
                uri=start_session(work,name,mime,size,existing=existing_id)
            except ArchiveError as exc:
                if existing_id and "HTTP 404" in str(exc):
                    uri=start_session(work,name,mime,size,existing=None)
                else:
                    raise
            position=0
        while position < size:
            if time.monotonic()-started > FILE_SLICE_SECONDS:
                gateway("release",task_id=work["task_id"],lease=work["lease"])
                return "yielded"
            end=min(size,position+CHUNK_BYTES)-1
            if body is not None:chunk=body[position:end+1]
            else:chunk=source_range(work,position,end,size)
            position,result=save_chunk(work,uri,position,chunk,size)
            gateway("progress",task_id=work["task_id"],lease=work["lease"],bytes=position)
        if result is None:
            _,result=session_status(work,uri,size)
            if result is None:raise ArchiveError("Google did not finalize the upload")
        created=result
    record=verify(work,created,size,src_md5)
    gateway("complete",task_id=work["task_id"],lease=work["lease"],
            destination_id=record["id"],destination_name=name,mime=mime,bytes=size,fidelity=fidelity)
    return "copied"


def execute_task():
    try:
        claimed=gateway("claim").get("work")
    except Exception as exc:
        LOG.warning("Claim error: %s",type(exc).__name__)
        return "gateway_error"
    if not claimed:return "empty"
    if claimed.get("skipped"):return "skipped"
    try:
        return archive_one(claimed)
    except Exception as exc:
        # Avoid logging OAuth tokens, file contents, resumable URIs.
        retry=getattr(exc,"retry",True)
        summary=str(exc)[:350]
        try:gateway("fail",task_id=claimed["task_id"],lease=claimed["lease"],error=summary,retry=retry)
        except Exception: LOG.warning("Unable to record file failure, lease will expire")
        LOG.warning("File task %s failed: %s",claimed["task_id"],type(exc).__name__)
        return "failed"


def run_once():
    started=time.monotonic();counter={"copied":0,"already":0,"yielded":0,"failed":0,"skipped":0,"gateway_error":0}
    # Each scheduler request advances the source Drive scan even while browsers are closed.
    # The old shared-host worker is used only for small scan batches, not file transfers.
    try:
        gateway("scan_step")
        gateway("folders_step",limit=2)
    except ArchiveError as exc:
        LOG.warning("Preflight scan/folder step: %s",type(exc).__name__)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while time.monotonic()-started < RUN_SECONDS:
            futures=[pool.submit(execute_task) for _ in range(WORKERS)]
            results=[f.result() for f in as_completed(futures)]
            for name in results:
                counter[name]=counter.get(name,0)+1
            if all(name in ("empty","gateway_error") for name in results):
                break
            if "gateway_error" in results:time.sleep(3)
    return {"ok":True,"results":counter,"elapsed_seconds":round(time.monotonic()-started,1),"workers":WORKERS}


@app.get("/health")
def health():
    return jsonify({"ok":True,"service":"InnovisionBD V12 Cloud Archive","gateway_configured":bool(SECRET),
                    "run_secret_configured":bool(RUN_SECRET),"workers":WORKERS})


@app.post("/run")
def run_endpoint():
    supplied=request.headers.get("X-Run-Secret","")
    if len(RUN_SECRET)<32 or not supplied or not __import__("hmac").compare_digest(RUN_SECRET,supplied):
        return jsonify({"ok":False,"error":"Forbidden"}),403
    try:return jsonify(run_once())
    except Exception:
        LOG.exception("Cloud archive runner failed")
        return jsonify({"ok":False,"error":"Internal worker failure; inspect Cloud Run logs"}),503


if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
