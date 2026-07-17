# ASCOM Alpaca Server for Ximea cameras (xiAPI)

A FastAPI-based server, implementing the ASCOM **ICameraV4** interface. Communication is via XIMEA's published xiAPI
library, loaded directly with ctypes — no vendor Python package required. Works on Windows, Linux and macOS wherever
the XIMEA software package is installed.

---

## Implemented ICameraV4 capabilities as of this driver version

| Capability           | Supported |
|----------------------|-----------|
| BayerOffsetX         | ✔         |
| BayerOffsetY         | ✔         |
| BinX                 | ✔         |
| BinY                 | ✔         |
| CameraState          | ✔         |
| CameraXSize          | ✔         |
| CameraYSize          | ✔         |
| CanAbortExposure     | ✔         |
| CanAsymmetricBin     | ✘         |
| CanFastReadout       | ✘         |
| CanGetCoolerPower    | ✘         |
| CanPulseGuide        | ✘         |
| CanSetCCDTemperature | ✔         |
| CanStopExposure      | ✔         |
| CCDTemperature       | ✔         |
| CoolerOn             | ✔         |
| CoolerPower          | ✘         |
| ElectronsPerADU      | ✘         |
| ExposureMax          | ✔         |
| ExposureMin          | ✔         |
| ExposureResolution   | ✔         |
| FastReadout          | ✘         |
| FullWellCapacity     | ✘         |
| Gain                 | ✔         |
| GainMax              | ✔         |
| GainMin              | ✔         |
| Gains                | ✘         |
| HasShutter           | ✔         |
| HeatSinkTemperature  | ✘         |
| ImageArray           | ✔         |
| ImageReady           | ✔         |
| IsPulseGuiding       | ✘         |
| LastExposureDuration | ✔         |
| MaxADU               | ✔         |
| MaxBinX              | ✔         |
| MaxBinY              | ✔         |
| NumX                 | ✔         |
| NumY                 | ✔         |
| Offset               | ✘         |
| OffsetMax            | ✘         |
| OffsetMin            | ✘         |
| Offsets              | ✘         |
| PercentCompleted     | ✘         |
| PixelSizeX           | ✔         |
| PixelSizeY           | ✔         |
| ReadoutMode          | ✔         |
| ReadoutModes         | ✔         |
| SensorName           | ✔         |
| SensorType           | ✔         |
| SetCCDTemperature    | ✔         |
| StartX               | ✔         |
| StartY               | ✔         |
| SubExposureDuration  | ✘         |
| AbortExposure        | ✔         |
| PulseGuide           | ✘         |
| StartExposure        | ✔         |
| StopExposure         | ✔         |

Camera capabilities (sensor size, gain range, exposure limits, cooling, color filter array) are
**introspected from xiAPI at connection time**, so any xiAPI-supported camera should work without
code changes. CoolerOn/SetCCDTemperature report NotImplemented on cameras without TEC cooling.
ASCOM Offset is not implemented because xiAPI's black level is read-only for live cameras.

---

## Architecture

| File               | Purpose                                     |
|--------------------|---------------------------------------------|
| `main.py`          | FastAPI app, lifespan, router wiring        |
| `config.py`        | Pydantic config models, YAML loader         |
| `config.yaml`      | User-editable configuration                 |
| `camera.py`        | FastAPI router – ICameraV4 endpoints        |
| `camera_device.py` | Low-level xiAPI driver                      |
| `libm3api.py`      | ctypes wrappers to the xiAPI library        |
| `management.py`    | `/management` Alpaca management endpoints   |
| `setup.py`         | `/setup` HTML stub pages                    |
| `discovery.py`     | UDP Alpaca discovery responder (port 32227) |
| `responses.py`     | Pydantic response models                    |
| `exceptions.py`    | ASCOM Alpaca error classes                  |
| `shr.py`           | Shared FastAPI dependencies / helpers       |
| `log.py`           | Loguru config + stdlib intercept handler    |
| `test.py`          | Quick smoke-test script                     |
| `requirements.txt` | Python package dependencies                 |
| `Dockerfile`       | Container build                             |

---

## Configuration

Edit `config.yaml` to match your camera setup. Example settings:

- `library`: Path to the xiAPI shared library (`libm3api.so.2` on Linux, `xiapi64.dll` on Windows,
  `m3api.framework/m3api` on macOS)
- `devices[].defaults`: Default temperature, readout mode, binning, gain (dB), pixel size (µm)

Camera properties (sensor size, gain/exposure ranges, cooling support) are
**queried from xiAPI at connection time** — no hardcoding required. Pixel size is the one
exception: xiAPI does not report it, so set `pixel_size` in `config.yaml` for your sensor.

Multiple Ximea cameras can be registered by adding further entries under
`devices:` with distinct `device_number` values.

## Quick start

```bash
pip install -r requirements.txt
python main.py
```

The server starts on `0.0.0.0:5920` by default (configurable in `config.yaml`).

---

## Smoke test

```bash
# Requires hardware connected, i.e. will operate camera
python test.py
```

---

## Docker

```bash
docker build -t alpaca-ximea .
docker run -d --name alpaca-ximea \
    -v ./config.yaml:/alpyca/config.yaml:ro \
    --privileged -v /dev/bus/usb:/dev/bus/usb \
    --network host \
    --restart unless-stopped \
    alpaca-ximea
docker logs -f alpaca-ximea
```

---

## ASCOM Conformance

<!-- conformu:start -->
Last tested with **ConformU 4.3.0 (Build 49708.0503dc7)** on 2026-07-17
(`python test_conformu.py`):

| Device | Errors | Issues | Info | Status |
|--------|:------:|:------:|:----:|:------:|
| Ximea_1 (Camera #0) | 1 | 0 | 279 | ✓ PASS |

_Errors may be non-zero when no hardware is attached (NotConnectedException is the expected response). **Issues == 0** indicates Alpaca protocol conformance._
<!-- conformu:end -->
