"""Append-blob debug logger (audit finding M9).

The old `write_debug` did a read-modify-write of the whole growing `debug/<job>.txt` on EVERY
call: download the full log, append one line, re-upload with overwrite=True. For a 70-image job
that calls it hundreds of times this is O(n^2) blob I/O, and a transient download failure reset
the accumulated log to just the new line.

`BlobDebugLog` appends each line to an Azure **Append Blob** instead: O(1) per call, no read, and
each line is persisted immediately — so the log survives an OOM SIGKILL (exit 137), which is
exactly the failure class we most need the log for (see C1/H8). The inference container runs ONE
job single-threaded, so no lock is needed.

Isolated here (no torch / diffusers imports; azure is imported lazily) so it is unit-testable
without importing the heavy `main` module.
"""
from __future__ import annotations


class BlobDebugLog:
    def __init__(self, blob_service, container):
        self._svc = blob_service
        self._container = container
        self._ready_blob = None   # blob_name for which the append blob has been (re)created

    def append(self, blob_name: str, line: str) -> None:
        client = self._svc.get_blob_client(container=self._container, blob=blob_name)
        if self._ready_blob != blob_name:
            # First write for this blob in this process: (re)create a FRESH, empty append blob.
            # overwrite=True starts each container run clean. Guarded so it happens once per run,
            # not once per line.
            try:
                from azure.storage.blob import BlobType
                blob_type = BlobType.AppendBlob
            except Exception:
                blob_type = "AppendBlob"   # test env without azure-storage-blob installed
            client.upload_blob(b"", blob_type=blob_type, overwrite=True)
            self._ready_blob = blob_name
        client.append_block(line.encode())
