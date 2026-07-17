from ctypes import (
    byref,
    c_float,
    c_int,
    c_uint,
    c_void_p,
    create_string_buffer,
    sizeof,
    string_at,
)
from datetime import datetime, timezone
from enum import IntEnum
from threading import Event, Lock, Thread
from typing import List, Optional

from astropy.time import Time
import numpy as np
import time

from libm3api import (
    XI_CFA,
    XI_DOWNSAMPLING_TYPE,
    XI_IMG,
    XI_IMG_FORMAT,
    XI_PRM,
    XI_TEMP_CTRL_MODE,
    XIError,
    load_m3api_library,
    xi_call,
)
from config import DeviceConfig
from log import get_logger


logger = get_logger()


class CameraState(IntEnum):
    IDLE = 0
    WAITING = 1
    EXPOSING = 2
    READING = 3
    DOWNLOADING = 4
    ERROR = 5


class SensorType(IntEnum):
    MONOCHROME = 0
    COLOR = 1
    RGGB = 2
    CMYG = 3
    CMYG2 = 4
    LRGB = 5


class CameraDevice:
    """Low-level driver for Ximea cameras (xiAPI / m3api)."""

    def __init__(self, device_config: DeviceConfig, library_path: str):
        self._lock = Lock()
        self._config = device_config
        self._library_path = library_path

        self._libm3api = None
        self._handle: Optional[c_void_p] = None

        self._connected = False
        self._connecting = False
        self._connect_thread: Optional[Thread] = None
        self._disconnect_thread: Optional[Thread] = None

        self._camera_state = CameraState.IDLE
        self._image_ready = False
        self._exposure_complete = Event()
        self._abort_requested = False

        self._last_exposure_duration: Optional[float] = None
        self._last_exposure_start_time: Optional[str] = None
        self._exposure_thread: Optional[Thread] = None

        self._image_buffer: Optional[bytes] = None

    #######################################
    # ASCOM Methods Common To All Devices #
    #######################################
    def connect(self) -> None:
        if self._connected or self._connecting:
            return
        self._connecting = True
        self._connect_thread = Thread(target=self._connect_worker, daemon=True)
        self._connect_thread.start()

    def _connect_worker(self) -> None:
        """Load the library, open the camera, query and set parameters."""
        try:
            # Load the library
            if self._libm3api is None:
                self._libm3api = load_m3api_library(self._library_path)
                if self._libm3api is None:
                    raise RuntimeError("Failed to load xiAPI library")

            # Discover cameras
            num_cameras = c_uint()
            xi_call(
                self._libm3api.xiGetNumberDevices,
                byref(num_cameras),
                operation="GetNumberDevices",
            )
            if num_cameras.value == 0:
                raise RuntimeError(
                    "No Ximea cameras found. "
                    "Check connection and ensure no other application has the camera open."
                )
            logger.info(f"Found {num_cameras.value} Ximea camera(s)")

            # Find our camera by serial number or use first available
            camera_index = 0
            camera_name = ""
            for i in range(num_cameras.value):
                name_buf = create_string_buffer(100)
                sn_buf = create_string_buffer(100)
                xi_call(
                    self._libm3api.xiGetDeviceInfoString,
                    c_uint(i),
                    XI_PRM.DEVICE_NAME.encode(),
                    name_buf,
                    c_uint(100),
                    operation="GetDeviceInfoString",
                )
                xi_call(
                    self._libm3api.xiGetDeviceInfoString,
                    c_uint(i),
                    XI_PRM.DEVICE_SN.encode(),
                    sn_buf,
                    c_uint(100),
                    operation="GetDeviceInfoString",
                )
                cam_name = name_buf.value.decode("utf-8", errors="replace").strip()
                cam_sn = sn_buf.value.decode("utf-8", errors="replace").strip()
                logger.debug(f"Camera {i}: {cam_name} (SN {cam_sn})")
                if self._config.serial_number:
                    if cam_sn == self._config.serial_number:
                        camera_index = i
                        camera_name = cam_name
                        break
                elif i == self._config.device_number:
                    camera_index = i
                    camera_name = cam_name
                    break

            # Open the camera
            handle = c_void_p()
            xi_call(
                self._libm3api.xiOpenDevice,
                c_uint(camera_index),
                byref(handle),
                operation="OpenDevice",
            )
            self._handle = handle
            self._sensor_name = camera_name

            # Now query camera properties from the SDK
            self._query_camera_properties()

            # Set remaining default parameters (temperature, gain, binning, etc.)
            self._set_default_parameters()

            self._connected = True
            self._camera_state = CameraState.IDLE
            self._image_ready = False
            logger.info(f"Connected to camera {self._config.entity}")

        except Exception as e:
            logger.error(f"Connection failed for {self._config.entity}: {e}")
            self._connected = False
            self._camera_state = CameraState.ERROR
            raise
        finally:
            self._connecting = False

    def _query_camera_properties(self) -> None:
        logger.debug(f"querying camera properties for {self._config.entity}")

        # API version
        api_ver = self._get_string(XI_PRM.API_VERSION, optional=True)
        logger.debug(f"xiAPI version: {api_ver}")

        # Color / Bayer
        self._is_color = bool(self._get_int_or(XI_PRM.IMAGE_IS_COLOR, 0))
        self._cfa = self._get_int_or(XI_PRM.COLOR_FILTER_ARRAY, XI_CFA.NONE)
        logger.debug(f"iscolor={self._is_color}, cfa={self._cfa}")

        # Cooler (TEC models only, e.g. xiD/xiX8; probe rather than assume)
        self._is_cooled = bool(self._get_int_or(XI_PRM.IS_COOLED, 0))
        logger.debug(f"iscooled={self._is_cooled}")

        # Downsampling means binning, not pixel skipping (where selectable)
        try:
            self._set_int(XI_PRM.DOWNSAMPLING_TYPE, XI_DOWNSAMPLING_TYPE.BINNING)
        except XIError:
            logger.debug("downsampling_type not settable, using camera default")

        # Frame size at 1x1 (downsampling changes width/height ranges)
        self._set_int(XI_PRM.DOWNSAMPLING, 1)
        self._camera_x_size = self._get_int(XI_PRM.WIDTH + XI_PRM.INFO_MAX)
        self._camera_y_size = self._get_int(XI_PRM.HEIGHT + XI_PRM.INFO_MAX)
        logger.debug(f"MaxWidth={self._camera_x_size}, MaxHeight={self._camera_y_size}")

        # Max binning (downsampling enum value == factor)
        self._max_bin = self._get_int_or(XI_PRM.DOWNSAMPLING + XI_PRM.INFO_MAX, 1)
        logger.debug(f"MaxDownsampling={self._max_bin}")

        # Pixel size (um) from config — xiAPI does not report pixel pitch
        self._pixel_size_x = self._config.defaults.pixel_size
        self._pixel_size_y = self._config.defaults.pixel_size

        # Image format: 16-bit preferred, raw for color sensors so the
        # image stays a 2D Bayer mosaic rather than interpolated RGB
        if self._is_color:
            format_candidates = [XI_IMG_FORMAT.RAW16, XI_IMG_FORMAT.RAW8]
        else:
            format_candidates = [XI_IMG_FORMAT.MONO16, XI_IMG_FORMAT.MONO8]
        self._img_format = None
        for fmt in format_candidates:
            try:
                self._set_int(XI_PRM.IMAGE_DATA_FORMAT, fmt)
                self._img_format = fmt
                break
            except XIError:
                continue
        if self._img_format is None:
            raise RuntimeError("Camera supports neither 16-bit nor 8-bit raw/mono format")
        logger.debug(f"imgdataformat={self._img_format}")

        # Bit depth (for MaxADU)
        default_depth = 16 if self._img_format in (XI_IMG_FORMAT.MONO16, XI_IMG_FORMAT.RAW16) else 8
        self._adc_bit_depth = self._get_int_or(XI_PRM.IMAGE_DATA_BIT_DEPTH, default_depth)
        logger.debug(f"BitDepth={self._adc_bit_depth}")

        # Exposure limits (SDK unit: microseconds)
        try:
            self._exposure_min = self._get_int(XI_PRM.EXPOSURE + XI_PRM.INFO_MIN) / 1_000_000.0
            self._exposure_max = self._get_int(XI_PRM.EXPOSURE + XI_PRM.INFO_MAX) / 1_000_000.0
        except XIError:
            self._exposure_min = 0.0
            self._exposure_max = 3600.0
        self._exposure_resolution = 0.000001  # 1 us
        logger.debug(f"exposure range [{self._exposure_min}, {self._exposure_max}] s")

        # Gain limits (SDK unit: dB, float)
        try:
            self._gain_min = self._get_float(XI_PRM.GAIN + XI_PRM.INFO_MIN)
            self._gain_max = self._get_float(XI_PRM.GAIN + XI_PRM.INFO_MAX)
        except XIError:
            self._gain_min = 0.0
            self._gain_max = 0.0
        logger.debug(f"gain range [{self._gain_min}, {self._gain_max}] dB")

        # Build readout modes from config (gain presets) or single default
        if self._config.readout_modes:
            self._readout_modes = [mode.label for mode in self._config.readout_modes]
            self._readout_mode_gains = [mode.gain for mode in self._config.readout_modes]
        else:
            self._readout_modes = [f"Gain_{self._config.defaults.gain}"]
            self._readout_mode_gains = [self._config.defaults.gain]

    def _set_default_parameters(self) -> None:
        logger.debug(f"setting default parameters for {self._config.entity}")
        defaults = self._config.defaults

        # Temperature target (cooled cameras only)
        if self._is_cooled:
            try:
                self._set_float(XI_PRM.TARGET_TEMP, float(defaults.temperature))
                self._set_int(XI_PRM.COOLING, XI_TEMP_CTRL_MODE.AUTO)
            except XIError as e:
                logger.warning(f"Unable to enable cooling: {e}")

        # Gain (dB)
        try:
            self._set_float(XI_PRM.GAIN, float(defaults.gain))
        except XIError as e:
            logger.warning(f"Unable to set default gain: {e}")

        # Readout mode index
        self._readout_mode = defaults.readout_mode

        # ROI: full frame at default binning
        self._bin = 1
        if defaults.binning != 1:
            try:
                self._set_int(XI_PRM.DOWNSAMPLING, defaults.binning)
                self._bin = defaults.binning
            except XIError:
                logger.warning(f"Default binning {defaults.binning} not supported, using 1")
                self._set_int(XI_PRM.DOWNSAMPLING, 1)

        self._set_full_frame()

    def _set_full_frame(self) -> None:
        """Reset ROI to full frame at the current downsampling."""
        self._set_int(XI_PRM.OFFSET_X, 0)
        self._set_int(XI_PRM.OFFSET_Y, 0)
        width = self._get_int(XI_PRM.WIDTH + XI_PRM.INFO_MAX)
        height = self._get_int(XI_PRM.HEIGHT + XI_PRM.INFO_MAX)
        self._set_int(XI_PRM.WIDTH, width)
        self._set_int(XI_PRM.HEIGHT, height)

        self._start_x = 0
        self._start_y = 0
        self._num_x = width
        self._num_y = height

    # ── xiAPI parameter helpers ──────────────────────────────────
    def _set_int(self, param: str, value: int) -> None:
        xi_call(
            self._libm3api.xiSetParamInt,
            self._handle,
            param.encode(),
            c_int(value),
            operation=f"SetParamInt({param}={value})",
        )

    def _get_int(self, param: str) -> int:
        value = c_int()
        xi_call(
            self._libm3api.xiGetParamInt,
            self._handle,
            param.encode(),
            byref(value),
            operation=f"GetParamInt({param})",
        )
        return int(value.value)

    def _get_int_or(self, param: str, default: int) -> int:
        """Probe an optional integer parameter, returning `default` when
        the camera does not support it."""
        try:
            return self._get_int(param)
        except XIError:
            return default

    def _set_float(self, param: str, value: float) -> None:
        xi_call(
            self._libm3api.xiSetParamFloat,
            self._handle,
            param.encode(),
            c_float(value),
            operation=f"SetParamFloat({param}={value})",
        )

    def _get_float(self, param: str) -> float:
        value = c_float()
        xi_call(
            self._libm3api.xiGetParamFloat,
            self._handle,
            param.encode(),
            byref(value),
            operation=f"GetParamFloat({param})",
        )
        return float(value.value)

    def _get_string(self, param: str, optional: bool = False) -> str:
        buf = create_string_buffer(256)
        try:
            xi_call(
                self._libm3api.xiGetParamString,
                self._handle,
                param.encode(),
                buf,
                c_uint(256),
                operation=f"GetParamString({param})",
            )
        except XIError:
            if optional:
                return ""
            raise
        return buf.value.decode("utf-8", errors="replace").strip()

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        if value and not self._connected:
            self.connect()
        elif not value and self._connected:
            self.disconnect()

    @property
    def connecting(self) -> bool:
        return self._connecting

    def disconnect(self) -> None:
        if not self._connected and not self._connecting:
            return
        self._disconnect_thread = Thread(target=self._disconnect_worker, daemon=True)
        self._disconnect_thread.start()

    def _disconnect_worker(self) -> None:
        try:
            if self._camera_state in (CameraState.EXPOSING, CameraState.READING):
                self.abort_exposure()
            if self._handle is not None and self._libm3api:
                self._libm3api.xiCloseDevice(self._handle)
                self._handle = None
            self._connected = False
            self._camera_state = CameraState.IDLE
            logger.info(f"Disconnected from camera {self._config.entity}")
        except Exception as e:
            logger.error(f"Disconnect error for {self._config.entity}: {e}")
        finally:
            self._connecting = False

    @property
    def entity(self) -> str:
        return self._config.entity

    ######################
    # ICamera properties #
    ######################
    @property
    def bin_x(self) -> int:
        return self._bin

    @bin_x.setter
    def bin_x(self, value: int) -> None:
        self._set_binning(value)

    @property
    def bin_y(self) -> int:
        return self._bin

    @bin_y.setter
    def bin_y(self, value: int) -> None:
        self._set_binning(value)

    def _set_binning(self, value: int) -> None:
        if value < 1 or value > self._max_bin:
            raise ValueError(f"Bin {value} out of range [1, {self._max_bin}]")
        try:
            self._set_int(XI_PRM.DOWNSAMPLING, value)
        except XIError:
            # Camera rejects intermediate factors (e.g. only 1x/2x/4x)
            raise ValueError(f"Bin {value} not supported by camera")
        self._bin = value

        # Reset to full frame at new binning
        self._set_full_frame()

    @property
    def camera_state(self) -> CameraState:
        return self._camera_state

    @property
    def camera_x_size(self) -> int:
        return self._camera_x_size

    @property
    def camera_y_size(self) -> int:
        return self._camera_y_size

    @property
    def can_abort_exposure(self) -> bool:
        return True

    @property
    def can_asymmetric_bin(self) -> bool:
        return False

    @property
    def can_fast_readout(self) -> bool:
        return False

    @property
    def can_get_cooler_power(self) -> bool:
        return False

    @property
    def can_pulse_guide(self) -> bool:
        return False

    @property
    def can_set_ccd_temperature(self) -> bool:
        return self._is_cooled

    @property
    def can_stop_exposure(self) -> bool:
        return True

    @property
    def ccd_temperature(self) -> float:
        for param in (XI_PRM.TEMP, XI_PRM.CHIP_TEMP):
            try:
                return self._get_float(param)
            except XIError:
                continue
        logger.warning("Unable to read temperature")
        return 99.0

    @property
    def cooler_on(self) -> bool:
        if not self._is_cooled:
            return False
        try:
            return self._get_int(XI_PRM.COOLING) != XI_TEMP_CTRL_MODE.OFF
        except XIError:
            return False

    @cooler_on.setter
    def cooler_on(self, value: bool) -> None:
        if self._is_cooled:
            self._set_int(
                XI_PRM.COOLING,
                XI_TEMP_CTRL_MODE.AUTO if value else XI_TEMP_CTRL_MODE.OFF,
            )

    @property
    def cooler_power(self) -> float:
        # xiAPI does not report TEC drive power
        return 0.0

    @property
    def exposure_max(self) -> float:
        return self._exposure_max

    @property
    def exposure_min(self) -> float:
        return self._exposure_min

    @property
    def exposure_resolution(self) -> float:
        return self._exposure_resolution

    @property
    def gain(self) -> int:
        try:
            return int(round(self._get_float(XI_PRM.GAIN)))
        except XIError:
            return 0

    @gain.setter
    def gain(self, value: int) -> None:
        if value < self._gain_min or value > self._gain_max:
            raise ValueError(f"Gain {value} out of range [{self._gain_min}, {self._gain_max}]")
        self._set_float(XI_PRM.GAIN, float(value))

    @property
    def gain_max(self) -> int:
        return int(self._gain_max)

    @property
    def gain_min(self) -> int:
        return int(self._gain_min)

    @property
    def has_offset(self) -> bool:
        # xiAPI's image_black_level is read-only for live cameras, so
        # ASCOM Offset cannot be implemented.
        return False

    @property
    def has_shutter(self) -> bool:
        # Electronic shutter only, no mechanical shutter on any model
        return False

    @property
    def image_array(self) -> np.ndarray:
        if not self._image_ready:
            raise RuntimeError("No image ready")
        if self._image_buffer is None:
            raise RuntimeError("No image data available")

        self._camera_state = CameraState.DOWNLOADING

        width, height = self._num_x, self._num_y

        if self._img_format in (XI_IMG_FORMAT.MONO16, XI_IMG_FORMAT.RAW16):
            img = np.frombuffer(self._image_buffer, dtype=np.uint16).reshape((height, width))
        else:
            img = np.frombuffer(self._image_buffer, dtype=np.uint8).reshape((height, width))

        logger.debug(
            f"got data with {img.shape} dtype={img.dtype}"
        )

        self._camera_state = CameraState.IDLE
        self._image_ready = False

        # Transpose from native (H, W) to ASCOM (W, H), preserving
        # native unsigned dtype (uint8 for MONO8/RAW8, uint16 for
        # MONO16/RAW16).
        return np.ascontiguousarray(img.swapaxes(0, 1))

    @property
    def image_ready(self) -> bool:
        return self._image_ready

    @property
    def last_exposure_duration(self) -> float:
        return self._last_exposure_duration

    @property
    def last_exposure_start_time(self) -> str:
        return self._last_exposure_start_time

    @property
    def max_adu(self) -> int:
        return int((1 << self._adc_bit_depth) - 1)

    @property
    def max_bin_x(self) -> int:
        return self._max_bin

    @property
    def max_bin_y(self) -> int:
        return self._max_bin

    @property
    def num_x(self) -> int:
        return self._num_x

    @num_x.setter
    def num_x(self, value: int) -> None:
        self._set_roi(num_x=value)

    @property
    def num_y(self) -> int:
        return self._num_y

    @num_y.setter
    def num_y(self, value: int) -> None:
        self._set_roi(num_y=value)

    @property
    def offset(self) -> int:
        return 0

    @property
    def offset_max(self) -> int:
        return 0

    @property
    def offset_min(self) -> int:
        return 0

    @property
    def pixel_size_x(self) -> float:
        return self._pixel_size_x

    @property
    def pixel_size_y(self) -> float:
        return self._pixel_size_y

    @property
    def readout_mode(self) -> int:
        return self._readout_mode

    @readout_mode.setter
    def readout_mode(self, value: int) -> None:
        if value < 0 or value >= len(self._readout_modes):
            raise ValueError(
                f"ReadoutMode {value} out of range [0, {len(self._readout_modes) - 1}]"
            )
        self._readout_mode = value
        gain = self._readout_mode_gains[value]
        self._set_float(XI_PRM.GAIN, float(gain))
        logger.info(f"Set readout mode to {self._readout_modes[value]} (gain={gain})")

    @property
    def readout_modes(self) -> List[str]:
        return self._readout_modes

    @property
    def sensor_name(self) -> str:
        return self._sensor_name

    @property
    def sensor_type(self) -> SensorType:
        if not self._is_color:
            return SensorType.MONOCHROME
        if self._cfa in (
            XI_CFA.BAYER_RGGB,
            XI_CFA.BAYER_BGGR,
            XI_CFA.BAYER_GRBG,
            XI_CFA.BAYER_GBRG,
        ):
            return SensorType.RGGB
        if self._cfa == XI_CFA.CMYG:
            return SensorType.CMYG
        return SensorType.COLOR

    @property
    def set_ccd_temperature(self) -> float:
        if not self._is_cooled:
            return 99.0
        try:
            return self._get_float(XI_PRM.TARGET_TEMP)
        except XIError:
            logger.warning("Unable to read temperature set point")
            return 99.0

    @set_ccd_temperature.setter
    def set_ccd_temperature(self, value: float) -> None:
        if self._is_cooled:
            try:
                self._set_float(XI_PRM.TARGET_TEMP, float(value))
                logger.debug(f"set ccd temperature to {value}")
            except XIError:
                logger.warning("Unable to set ccd temperature")

    @property
    def start_x(self) -> int:
        return self._start_x

    @start_x.setter
    def start_x(self, value: int) -> None:
        self._set_roi(start_x=value)

    @property
    def start_y(self) -> int:
        return self._start_y

    @start_y.setter
    def start_y(self, value: int) -> None:
        self._set_roi(start_y=value)

    @property
    def timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _set_roi(
        self, start_x=None, num_x=None, start_y=None, num_y=None
    ) -> None:
        """
        Set ROI with proper validation.

        All start/num values are in binned pixels per ASCOM spec.
        xiAPI also works in binned pixels for width/height/offsetX/offsetY
        (after downsampling is set), but requires each to be a multiple
        of its own ":inc" increment, queried live below.
        """
        sx = start_x if start_x is not None else self._start_x
        sy = start_y if start_y is not None else self._start_y
        nx = num_x if num_x is not None else self._num_x
        ny = num_y if num_y is not None else self._num_y

        # Max binned dimensions (offsets zeroed first so width:max /
        # height:max report the full binned frame)
        self._set_int(XI_PRM.OFFSET_X, 0)
        self._set_int(XI_PRM.OFFSET_Y, 0)
        max_binned_x = self._get_int(XI_PRM.WIDTH + XI_PRM.INFO_MAX)
        max_binned_y = self._get_int(XI_PRM.HEIGHT + XI_PRM.INFO_MAX)

        # Validate and clamp start values
        if sx < 0:
            sx = 0
        if sy < 0:
            sy = 0
        if sx >= max_binned_x:
            sx = max_binned_x - 1
        if sy >= max_binned_y:
            sy = max_binned_y - 1

        # Validate and clamp num values
        max_nx = max_binned_x - sx
        max_ny = max_binned_y - sy
        if nx < 1:
            nx = 1
        if ny < 1:
            ny = 1
        if nx > max_nx:
            nx = max_nx
        if ny > max_ny:
            ny = max_ny

        # xiAPI alignment: each geometry parameter has its own increment
        width_inc = self._get_int_or(XI_PRM.WIDTH + XI_PRM.INFO_INCREMENT, 1) or 1
        height_inc = self._get_int_or(XI_PRM.HEIGHT + XI_PRM.INFO_INCREMENT, 1) or 1
        offset_x_inc = self._get_int_or(XI_PRM.OFFSET_X + XI_PRM.INFO_INCREMENT, 1) or 1
        offset_y_inc = self._get_int_or(XI_PRM.OFFSET_Y + XI_PRM.INFO_INCREMENT, 1) or 1

        min_nx = self._get_int_or(XI_PRM.WIDTH + XI_PRM.INFO_MIN, width_inc)
        min_ny = self._get_int_or(XI_PRM.HEIGHT + XI_PRM.INFO_MIN, height_inc)

        nx = (nx // width_inc) * width_inc
        ny = (ny // height_inc) * height_inc
        if nx < min_nx:
            nx = min_nx
        if ny < min_ny:
            ny = min_ny
        sx = (sx // offset_x_inc) * offset_x_inc
        sy = (sy // offset_y_inc) * offset_y_inc
        if sx + nx > max_binned_x:
            sx = ((max_binned_x - nx) // offset_x_inc) * offset_x_inc
        if sy + ny > max_binned_y:
            sy = ((max_binned_y - ny) // offset_y_inc) * offset_y_inc

        # Set size first (offsets are already zeroed), then position
        self._set_int(XI_PRM.WIDTH, nx)
        self._set_int(XI_PRM.HEIGHT, ny)
        self._set_int(XI_PRM.OFFSET_X, sx)
        self._set_int(XI_PRM.OFFSET_Y, sy)

        self._start_x = sx
        self._start_y = sy
        self._num_x = nx
        self._num_y = ny

    ###################
    # ICamera methods #
    ###################
    def start_exposure(self, duration: float, light: bool) -> None:
        if self._camera_state != CameraState.IDLE:
            raise RuntimeError("Camera is not idle")
        self._image_ready = False
        self._abort_requested = False
        self._camera_state = CameraState.WAITING
        self._exposure_complete.clear()
        self._exposure_thread = Thread(
            target=self._exposure_worker, args=(duration, light), daemon=True
        )
        self._exposure_thread.start()

    def _exposure_worker(self, duration: float, light: bool) -> None:
        acquiring = False
        try:
            # Set exposure time (SDK unit: microseconds). The `light`
            # flag is ignored — Ximea cameras have no mechanical shutter,
            # so darks are taken by capping the lens.
            exposure_us = int(duration * 1_000_000)
            self._set_int(XI_PRM.EXPOSURE, exposure_us)

            self._last_exposure_start_time = Time.now().isot
            self._last_exposure_duration = duration

            # Start free-run acquisition; the first frame delivered is
            # the first full exposure after this call
            xi_call(
                self._libm3api.xiStartAcquisition,
                self._handle,
                operation="StartAcquisition",
            )
            acquiring = True

            self._camera_state = CameraState.EXPOSING
            logger.debug(f"starting exposure ({duration}s)")

            # Block for the frame (timeout in ms, exposure + margin)
            img = XI_IMG()
            img.size = sizeof(XI_IMG)
            timeout_ms = int(duration * 1000) + 60_000
            try:
                xi_call(
                    self._libm3api.xiGetImage,
                    self._handle,
                    c_uint(timeout_ms),
                    byref(img),
                    operation="GetImage",
                )
            except XIError:
                if self._abort_requested:
                    logger.debug("exposure aborted")
                    return
                raise

            logger.debug("exposure complete, reading data")
            self._camera_state = CameraState.READING

            # Copy out of the API-owned circular buffer immediately
            # (XI_BP_UNSAFE default: bp is only valid until later frames)
            raw = string_at(img.bp, img.bp_size)

            # Strip per-line padding if the transport added any
            width, height = int(img.width), int(img.height)
            bytes_per_pixel = 2 if self._img_format in (XI_IMG_FORMAT.MONO16, XI_IMG_FORMAT.RAW16) else 1
            padding_x = int(img.padding_x)
            if padding_x:
                row_bytes = width * bytes_per_pixel + padding_x
                rows = np.frombuffer(raw, dtype=np.uint8)[: height * row_bytes]
                raw = rows.reshape(height, row_bytes)[:, : width * bytes_per_pixel].tobytes()

            self._image_buffer = raw
            self._exposure_complete.set()
            self._image_ready = True
            logger.debug("image ready")

        except Exception as e:
            logger.error(f"Exposure failed: {e}")
            self._camera_state = CameraState.ERROR
            self._image_ready = False
        finally:
            if acquiring:
                try:
                    self._libm3api.xiStopAcquisition(self._handle)
                except Exception:
                    logger.warning("Unable to stop acquisition")
            if self._camera_state == CameraState.READING:
                self._camera_state = CameraState.IDLE

    def abort_exposure(self) -> None:
        if self._camera_state in (
            CameraState.EXPOSING,
            CameraState.READING,
            CameraState.WAITING,
        ):
            self._abort_requested = True
            try:
                # Unblocks the worker's xiGetImage with an error
                self._libm3api.xiStopAcquisition(self._handle)
            except Exception:
                logger.warning("Unable to abort exposure")
                pass
            self._camera_state = CameraState.IDLE
            self._image_ready = False
            self._exposure_complete.set()

    def stop_exposure(self) -> None:
        """Stop exposure — xiAPI has no partial readout, so stop aborts."""
        self.abort_exposure()

    def pulse_guide(self, direction: int, duration_ms: int) -> None:
        """Ximea cameras have no ST4 guide port."""
        raise RuntimeError("Camera does not have ST4 port")
