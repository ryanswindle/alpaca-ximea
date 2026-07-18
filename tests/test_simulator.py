"""
Hardware-free integration test against the xiAPI **camera simulator**.

Stands up the real alpaca-ximea server with one (or more) simulated
cameras and exercises it end-to-end through the alpyca client: connect,
property reads, two exposures, and both image-transfer paths
(ImageArrayRaw and JSON ImageArray), then disconnect.

Requires the XIMEA software package (xiAPI / m3api) to be installed.
If the library can't be located or loaded — or the installed build
doesn't offer the simulator — the test SKIPs with exit code 0, so it is
safe to run on machines without the SDK.

The simulator is enabled purely from this harness; nothing in src/ knows
about it. `cam_simulators_count` is a process-wide xiAPI setting applied
on a NULL handle before enumeration. Because ctypes' CDLL() returns the
same dlopen image within a process, the server's own driver — running in
a background thread of this same process — sees the simulated cameras.

Usage:
    python tests/test_simulator.py [--simulators N] [--port PORT]

Pass criterion: prints "PASS" and exits 0. SKIP (no SDK) is also exit 0;
any assertion failure exits 1.
"""

import argparse
import os
import sys
import threading
import time
import urllib.request
from ctypes import c_int
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np


# array.array typecode → numpy dtype (mirrors tests/test.py so the wire
# dtype is preserved through ImageArrayRaw).
_TYPECODE_TO_DTYPE = {
    "B": "uint8",
    "h": "int16",
    "H": "uint16",
    "i": "int32",
    "I": "uint32",
}


def find_xiapi_library(configured: str) -> str:
    """Locate the xiAPI (m3api) shared library across platforms.

    Tries the configured path first, then the standard install
    locations, then the OS loader search. Returns "" if none is found.
    """
    candidates = []
    if configured and os.path.exists(configured):
        candidates.append(configured)
    candidates += [
        "/Library/Frameworks/m3api.framework/m3api",  # macOS
        "/usr/lib/libm3api.so.2",                     # Linux
        "/usr/local/lib/libm3api.so.2",
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    from ctypes.util import find_library
    for name in ("xiapi64", "m3api", "xiapi32"):
        found = find_library(name)
        if found:
            return found
    return ""


def wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    url = f"http://{host}:{port}/management/v1/configureddevices"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _await(pred, what: str, timeout: float = 30.0) -> None:
    t0 = time.monotonic()
    while not pred():
        time.sleep(0.05)
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(f"timed out waiting for {what}")


def run_client(host: str, port: int) -> None:
    """Drive the running server through alpyca and assert invariants."""
    from alpaca.camera import Camera

    cam = Camera(f"{host}:{port}", 0)
    print(f"  Name={cam.Name!r}  Driver={cam.DriverVersion}  Interface={cam.InterfaceVersion}")

    cam.Connected = True
    _await(lambda: cam.Connected, "connect")
    print("  connected")

    nx, ny = cam.NumX, cam.NumY
    assert nx > 0 and ny > 0, f"bad frame size {nx}x{ny}"
    print(
        f"  sensor={cam.SensorName!r} type={cam.SensorType} "
        f"frame={nx}x{ny} maxadu={cam.MaxADU} "
        f"gain[{cam.GainMin},{cam.GainMax}] exp[{cam.ExposureMin},{cam.ExposureMax}]"
    )

    # Exposure #1 → ImageArrayRaw (typed binary path; how SensorKit pulls frames)
    cam.StartExposure(0.2, True)
    _await(lambda: cam.ImageReady, "exposure #1")
    raw = cam.ImageArrayRaw
    info = cam.ImageArrayInfo
    dtype = _TYPECODE_TO_DTYPE.get(raw.typecode, "uint16")
    arr = np.ascontiguousarray(
        np.frombuffer(raw, dtype=dtype).reshape(info.Dimension1, info.Dimension2).T
    )
    assert (info.Dimension1, info.Dimension2) == (nx, ny), (
        f"ImageArrayRaw dims {info.Dimension1}x{info.Dimension2} != NumX,NumY {nx}x{ny}"
    )
    assert arr.shape == (ny, nx), f"reconstructed shape {arr.shape} != {(ny, nx)}"
    print(
        f"  ImageArrayRaw : {info.Dimension1}x{info.Dimension2} "
        f"typecode={raw.typecode!r} -> shape={arr.shape} dtype={arr.dtype}"
    )

    # Exposure #2 → JSON ImageArray (ImageArray is one-shot: the server
    # flips ImageReady->False once a frame is retrieved).
    cam.StartExposure(0.1, True)
    _await(lambda: cam.ImageReady, "exposure #2")
    ia = np.array(cam.ImageArray)
    assert ia.shape == (nx, ny), f"ImageArray shape {ia.shape} != NumX,NumY {nx}x{ny}"
    print(f"  ImageArray(JSON): shape={ia.shape} dtype={ia.dtype}")

    cam.Connected = False
    _await(lambda: not cam.Connected, "disconnect")
    print("  disconnected")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulators", type=int, default=1, help="number of simulated cameras")
    parser.add_argument("--port", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import config
    from libm3api import load_m3api_library

    lib_path = find_xiapi_library(config.config.library)
    if not lib_path:
        print("SKIP: xiAPI (m3api) library not found — install the XIMEA package to run this test.")
        return 0

    lib = load_m3api_library(lib_path)
    if lib is None:
        print(f"SKIP: xiAPI library at {lib_path} could not be loaded.")
        return 0

    # Enable the simulator BEFORE the server enumerates (process-wide, NULL handle).
    rc = lib.xiSetParamInt(None, b"cam_simulators_count", c_int(args.simulators))
    if rc != 0:
        print(f"SKIP: this xiAPI build rejected cam_simulators_count (rc={rc}).")
        return 0
    print(f"xiAPI library: {lib_path}")
    print(f"simulator:     cam_simulators_count={args.simulators}")

    # Point the server driver at the located library for this run and pick the port.
    config.config.library = lib_path
    port = args.port or config.config.server.port
    print(f"server:        http://{args.host}:{port}\n")

    # Start the server in a background thread (same process → shared dlopen
    # image, so the driver sees the simulated cameras). uvicorn's signal
    # handlers only work on the main thread, so disable them here.
    import uvicorn
    import main as server_main

    uconf = uvicorn.Config(server_main.app, host=args.host, port=port, log_level="warning")
    server = uvicorn.Server(uconf)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    ok = False
    try:
        if not wait_for_server(args.host, port):
            print("FAIL: server did not become ready within 15s")
            return 1
        run_client(args.host, port)
        ok = True
    except AssertionError as e:
        print(f"FAIL: assertion failed — {e}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    print()
    print("=" * 56)
    print("PASS" if ok else "FAIL")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
