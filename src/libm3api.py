import os
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_char_p,
    c_double,
    c_float,
    c_int,
    c_uint,
    c_void_p,
)
import sys
from typing import Optional

from log import get_logger

logger = get_logger()

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")


# ──────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────
class XI_IMG_FORMAT:
    MONO8 = 0
    MONO16 = 1
    RGB24 = 2
    RGB32 = 3
    RGB_PLANAR = 4
    RAW8 = 5
    RAW16 = 6
    FRM_TRANSPORT_DATA = 7


class XI_CFA:
    NONE = 0
    BAYER_RGGB = 1
    CMYG = 2
    RGR = 3
    BAYER_BGGR = 4
    BAYER_GRBG = 5
    BAYER_GBRG = 6


class XI_BP:
    UNSAFE = 0
    SAFE = 1


class XI_TRG_SOURCE:
    OFF = 0
    EDGE_RISING = 1
    EDGE_FALLING = 2
    SOFTWARE = 3


class XI_SWITCH:
    OFF = 0
    ON = 1


class XI_TEMP_CTRL_MODE:
    OFF = 0
    AUTO = 1
    MANUAL = 2


class XI_DOWNSAMPLING_TYPE:
    BINNING = 0
    SKIPPING = 1


class XI_RET:
    OK = 0
    TIMEOUT = 10
    NOT_SUPPORTED = 12
    ACQUISITION_STOPED = 45
    NO_DEVICES_FOUND = 56
    UNKNOWN_PARAM = 100
    WRONG_PARAM_VALUE = 101
    NOT_SUPPORTED_PARAM = 106
    NOT_SUPPORTED_DATA_FORMAT = 108


XI_ERROR_NAMES = {
    0: "Ok",
    1: "InvalidHandle",
    2: "ReadReg",
    3: "WriteReg",
    4: "FreeResources",
    5: "FreeChannel",
    6: "FreeBandwidth",
    7: "ReadBlk",
    8: "WriteBlk",
    9: "NoImage",
    10: "Timeout",
    11: "InvalidArg",
    12: "NotSupported",
    13: "IsochAttachBuffers",
    14: "GetOverlappedResult",
    15: "MemoryAllocation",
    16: "DllContextIsNull",
    17: "DllContextIsNonZero",
    18: "DllContextExist",
    19: "TooManyDevices",
    20: "ErrorCamContext",
    21: "UnknownHardware",
    22: "InvalidTmFile",
    23: "InvalidTmTag",
    24: "IncompleteTm",
    25: "BusResetFailed",
    26: "NotImplemented",
    27: "ShadingTooBright",
    28: "ShadingTooDark",
    29: "TooLowGain",
    30: "InvalidBpl",
    31: "BplRealloc",
    32: "InvalidPixelList",
    33: "InvalidFfs",
    34: "InvalidProfile",
    35: "InvalidCalibration",
    36: "InvalidBuffer",
    38: "InvalidData",
    39: "TgBusy",
    40: "IoWrong",
    41: "AcquisitionAlreadyUp",
    42: "OldDriverVersion",
    43: "GetLastError",
    44: "CantProcess",
    45: "AcquisitionStopped",
    46: "AcquisitionStoppedWithError",
    47: "InvalidInputIccProfile",
    48: "InvalidOutputIccProfile",
    49: "DeviceNotReady",
    50: "ShadingTooContrast",
    51: "AlreadyInitialized",
    52: "NotEnoughPrivileges",
    53: "NotCompatibleDriver",
    54: "TmInvalidResource",
    55: "DeviceHasBeenReset",
    56: "NoDevicesFound",
    57: "ResourceOrFunctionLocked",
    58: "BufferSizeTooSmall",
    59: "CouldntInitProcessor",
    60: "NotInitialized",
    61: "ResourceNotFound",
    100: "UnknownParam",
    101: "WrongParamValue",
    103: "WrongParamType",
    104: "WrongParamSize",
    105: "BufferTooSmall",
    106: "NotSupportedParam",
    107: "NotSupportedParamInfo",
    108: "NotSupportedDataFormat",
    109: "ReadOnlyParam",
    111: "BandwidthNotSupported",
    112: "InvalidFfsFileName",
    113: "FfsFileNotFound",
    114: "ParamNotSettable",
    115: "SafePolicyNotSupported",
    116: "GpuDirectNotAvailable",
    117: "IncorrectSensIdCheck",
    118: "IncorrectFpgaType",
    119: "ParamConditionallyNotAvailable",
    120: "ErrFrameBufferRamInit",
    201: "ProcOtherError",
    202: "ProcProcessingError",
    203: "ProcInputFormatUnsupported",
    204: "ProcOutputFormatUnsupported",
    205: "OutOfRange",
}


# ──────────────────────────────────────────────────────────────────
# Parameter name strings (XI_PRM_*)
# ──────────────────────────────────────────────────────────────────
class XI_PRM:
    EXPOSURE = "exposure"                       # integer, microseconds
    GAIN = "gain"                               # float, dB
    WIDTH = "width"
    HEIGHT = "height"
    OFFSET_X = "offsetX"                        # camelCase per xiApi.h
    OFFSET_Y = "offsetY"
    IMAGE_DATA_FORMAT = "imgdataformat"
    IMAGE_DATA_BIT_DEPTH = "image_data_bit_depth"
    SENSOR_BIT_DEPTH = "sensor_bit_depth"
    DOWNSAMPLING = "downsampling"               # enum, value == factor
    DOWNSAMPLING_TYPE = "downsampling_type"
    DEVICE_NAME = "device_name"
    DEVICE_SN = "device_sn"
    IMAGE_IS_COLOR = "iscolor"
    COLOR_FILTER_ARRAY = "cfa"
    IS_COOLED = "iscooled"
    COOLING = "cooling"                         # enum XI_TEMP_CTRL_MODE
    TARGET_TEMP = "target_temp"                 # float, degrees C
    CHIP_TEMP = "chip_temp"                     # float, degrees C
    TEMP = "temp"                               # float, degrees C (temp_selector)
    API_VERSION = "api_version"
    # Parameter-info modifier suffixes, appended to a parameter name
    # (e.g. "exposure:min"). Note increment is ":inc", not ":increment".
    INFO_MIN = ":min"
    INFO_MAX = ":max"
    INFO_INCREMENT = ":inc"


# ──────────────────────────────────────────────────────────────────
# Structures
# ──────────────────────────────────────────────────────────────────
class XI_IMG_DESC(Structure):
    _fields_ = [
        ("Area0Left", c_uint),
        ("Area1Left", c_uint),
        ("Area2Left", c_uint),
        ("Area3Left", c_uint),
        ("Area4Left", c_uint),
        ("Area5Left", c_uint),
        ("ActiveAreaWidth", c_uint),
        ("Area5Right", c_uint),
        ("Area4Right", c_uint),
        ("Area3Right", c_uint),
        ("Area2Right", c_uint),
        ("Area1Right", c_uint),
        ("Area0Right", c_uint),
        ("Area0Top", c_uint),
        ("Area1Top", c_uint),
        ("Area2Top", c_uint),
        ("Area3Top", c_uint),
        ("Area4Top", c_uint),
        ("Area5Top", c_uint),
        ("ActiveAreaHeight", c_uint),
        ("Area5Bottom", c_uint),
        ("Area4Bottom", c_uint),
        ("Area3Bottom", c_uint),
        ("Area2Bottom", c_uint),
        ("Area1Bottom", c_uint),
        ("Area0Bottom", c_uint),
        ("format", c_uint),
        ("flags", c_uint),
    ]


class XI_IMG(Structure):
    """Image descriptor for xiGetImage.

    Versioned struct: `size` must be set to sizeof(XI_IMG) before the
    call; the API fills fields only up to the declared size, so older
    (smaller) layouts remain accepted. Field order matches xiApi.h /
    official xiPython 4.27 exactly — do not reorder.
    """

    _fields_ = [
        ("size", c_uint),
        ("bp", c_void_p),
        ("bp_size", c_uint),
        ("frm", c_uint),
        ("width", c_uint),
        ("height", c_uint),
        ("nframe", c_uint),
        ("tsSec", c_uint),
        ("tsUSec", c_uint),
        ("GPI_level", c_uint),
        ("black_level", c_uint),
        ("padding_x", c_uint),
        ("AbsoluteOffsetX", c_uint),
        ("AbsoluteOffsetY", c_uint),
        ("transport_frm", c_uint),
        ("img_desc", XI_IMG_DESC),
        ("DownsamplingX", c_uint),
        ("DownsamplingY", c_uint),
        ("flags", c_uint),
        ("exposure_time_us", c_uint),
        ("gain_db", c_float),
        ("acq_nframe", c_uint),
        ("image_user_data", c_uint),
        ("exposure_sub_times_us", c_uint * 5),
        ("data_saturation", c_double),
        ("wb_red", c_float),
        ("wb_green", c_float),
        ("wb_blue", c_float),
        ("lg_black_level", c_uint),
        ("hg_black_level", c_uint),
        ("lg_range", c_uint),
        ("hg_range", c_uint),
        ("gain_ratio", c_float),
        ("fDownsamplingX", c_float),
        ("fDownsamplingY", c_float),
        ("color_filter_array", c_uint),
    ]


# ──────────────────────────────────────────────────────────────────
# Error handling
# ──────────────────────────────────────────────────────────────────
def xi_error_string(error_code: int) -> str:
    return f"XI_{XI_ERROR_NAMES.get(error_code, f'Unknown({error_code})')}"


class XIError(Exception):
    def __init__(self, error_code: int, operation: str = ""):
        self.error_code = error_code
        self.error_string = xi_error_string(error_code)
        self.operation = operation
        message = f"XI error {error_code}: {self.error_string}"
        if operation:
            message = f"{operation}: {message}"
        super().__init__(message)


def xi_call(func, *args, operation: str = ""):
    error = func(*args)
    if error != 0:
        raise XIError(error, operation or func.__name__)
    return error


# ──────────────────────────────────────────────────────────────────
# Argtypes / restypes
# ──────────────────────────────────────────────────────────────────
_RE = c_int  # XI_RETURN


def _configure_argtypes(lib: CDLL) -> None:
    """Declare argtypes and restype for every xiAPI function we call.

    Without these, ctypes on 64-bit platforms silently mangles pointer
    arguments, and xiSetParamFloat's c_float argument gets promoted to
    a C double — both lead to garbage values or segfaults inside the
    native library.
    """

    # --- Camera discovery ---
    lib.xiGetNumberDevices.argtypes = [POINTER(c_uint)]
    lib.xiGetNumberDevices.restype = _RE

    lib.xiGetDeviceInfoString.argtypes = [c_uint, c_char_p, c_char_p, c_uint]
    lib.xiGetDeviceInfoString.restype = _RE

    # --- Open / Close ---
    lib.xiOpenDevice.argtypes = [c_uint, POINTER(c_void_p)]
    lib.xiOpenDevice.restype = _RE

    lib.xiCloseDevice.argtypes = [c_void_p]
    lib.xiCloseDevice.restype = _RE

    # --- Parameters (typed convenience wrappers) ---
    lib.xiSetParamInt.argtypes = [c_void_p, c_char_p, c_int]
    lib.xiSetParamInt.restype = _RE

    lib.xiSetParamFloat.argtypes = [c_void_p, c_char_p, c_float]
    lib.xiSetParamFloat.restype = _RE

    lib.xiGetParamInt.argtypes = [c_void_p, c_char_p, POINTER(c_int)]
    lib.xiGetParamInt.restype = _RE

    lib.xiGetParamFloat.argtypes = [c_void_p, c_char_p, POINTER(c_float)]
    lib.xiGetParamFloat.restype = _RE

    lib.xiGetParamString.argtypes = [c_void_p, c_char_p, c_void_p, c_uint]
    lib.xiGetParamString.restype = _RE

    # --- Acquisition ---
    lib.xiStartAcquisition.argtypes = [c_void_p]
    lib.xiStartAcquisition.restype = _RE

    lib.xiStopAcquisition.argtypes = [c_void_p]
    lib.xiStopAcquisition.restype = _RE

    lib.xiGetImage.argtypes = [c_void_p, c_uint, POINTER(XI_IMG)]
    lib.xiGetImage.restype = _RE

    logger.debug("xiAPI argtypes configured for all functions")


# ──────────────────────────────────────────────────────────────────
# Library loader
# ──────────────────────────────────────────────────────────────────
def load_m3api_library(library: str) -> Optional[CDLL]:
    """Load the xiAPI library (m3api).

    xiAPI is __cdecl on all platforms, so plain CDLL is correct even
    for xiapi64.dll on Windows (the official xiPython does the same).
    """
    try:
        lib = CDLL(library)

        logger.debug(f"loaded xiAPI library from {library}")
        _configure_argtypes(lib)
        return lib
    except OSError as e:
        logger.error(f"Failed to load xiAPI library from {library}: {e}")
        return None
