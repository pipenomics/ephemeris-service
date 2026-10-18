"""Modal entry for the ephemeris service: the same server.py, behind HTTPS, scaled to zero.

    EPHEMERIS_SERVICE_SRC=/path/to/ephemeris-service modal deploy modal_app.py
    modal deploy modal_app.py          # from inside the service folder

core.py and server.py are unchanged; this file only puts them in a container and starts
server.py on the port Modal forwards. The API key comes from the Modal secret
`ephemeris-key` (env var EPHEMERIS_API_KEY) and is checked by server.py on every request
as before. server.py logs method, path and status only, never a body, and Modal's own
request log carries no bodies either.

Licensed AGPL-3.0, see LICENSE.
"""
import os
import subprocess
from pathlib import Path

import modal

PORT = 8765
image = modal.Image.debian_slim(python_version="3.12").pip_install("pyswisseph==2.10.3.2")

# Modal imports this module again inside the container, where the local source does not
# exist, so the source lookup and the mounts run only on the deploying machine. The
# service source is this file's own folder once it lives in the service repo, or the
# folder named by EPHEMERIS_SERVICE_SRC when deployed from somewhere else.
if modal.is_local():
    SRC = Path(os.environ.get("EPHEMERIS_SERVICE_SRC") or Path(__file__).resolve().parent)
    if not (SRC / "server.py").exists() or not (SRC / "core.py").exists():
        raise SystemExit(f"server.py and core.py not found in {SRC}; set EPHEMERIS_SERVICE_SRC")
    image = (image
             .add_local_file(SRC / "core.py", "/root/service/core.py")
             .add_local_file(SRC / "server.py", "/root/service/server.py"))

app = modal.App("ephemeris-service")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("ephemeris-key")],
    cpu=0.25,
    memory=512,
    min_containers=0,
    scaledown_window=60,
    timeout=120,
)
@modal.concurrent(max_inputs=8)
@modal.web_server(PORT, startup_timeout=30)
def serve():
    # server.py keeps its localhost default; inside the container it must listen on all
    # interfaces so Modal's proxy can reach it. TLS is terminated by Modal.
    subprocess.Popen(
        ["python3", "-I", "/root/service/server.py", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd="/root/service",
    )
