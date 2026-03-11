# -*- coding: utf-8 -*-
# Patched copy of Utils/Console/protobuf/config_pb2.py
# Change: proto file name 'config/config.proto' → 'sensor/config.proto'
#         (same byte length) to avoid descriptor-pool conflict with the
#         stream protobuf's config_pb2.
# Change: _runtime_version guard removed for protobuf < 5.x compatibility.
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder

_sym_db = _symbol_database.Default()

# Original serialized bytes from Console/protobuf/config_pb2.py with the
# proto file name patched in-place (both strings are 19 bytes, same length).
_SERIALIZED = b'\n\x13\x63onfig/config.proto\x12\x02\x62m\"\xdd\x02\n\x0fNetworkConfigV4\x12\x15\n\rntp_server_ip\x18\x01 \x01(\x07\x12\x11\n\tserver_ip\x18\x02 \x01(\x07\x12\x13\n\x0bserver_port\x18\x03 \x01(\x07\x12\n\n\x02ip\x18\x04 \x01(\x07\x12\x0f\n\x07netmask\x18\x05 \x01(\x07\x12\x0f\n\x07gateway\x18\x06 \x01(\x07\x12\x16\n\x0entp_interval_s\x18\x07 \x01(\r\x12\x15\n\rntp_offset_us\x18\x08 \x01(\x05\x12\x19\n\x11has_ntp_offset_us\x18\x0c \x01(\x08\x12\x1f\n\x04\x64hcp\x18\t \x01(\x0e\x32\x11.bm.FeatureToggle\x12&\n\x0b\x64\x61ta_stream\x18\n \x01(\x0e\x32\x11.bm.FeatureToggle\x12\"\n\x1antp_min_ms_error_to_update\x18\x0b \x01(\r\x12&\n\x1ehas_ntp_min_ms_error_to_update\x18\r \x01(\x08\"_\n\x0c\x46ilterConfig\x12)\n\x0e\x66ilter_enabled\x18\x01 \x01(\x0e\x32\x11.bm.FilterEnabled\x12$\n\rfilter_cutoff\x18\x02 \x01(\x0e\x32\r.bm.CutoffKHz\"\x91\x01\n\x0cSensorConfig\x12&\n\nfull_scale\x18\x01 \x01(\x0e\x32\x12.bm.AccelFullScale\x12 \n\x06\x66ilter\x18\x02 \x01(\x0b\x32\x10.bm.FilterConfig\x12\x1a\n\x04\x61xes\x18\x03 \x01(\x0e\x32\x0c.bm.AxisMask\x12\x1b\n\x07odr_div\x18\x04 \x01(\x0e\x32\n.bm.OdrDiv\"\xc8\x01\n\nSensorInfo\x12\x13\n\x0bsensor_type\x18\x01 \x01(\r\x12\x18\n\x10hardware_version\x18\x02 \x01(\r\x12\x18\n\x10\x66irmware_version\x18\x03 \x01(\r\x12\x1a\n\x12\x62ootloader_version\x18\x04 \x01(\r\x12\r\n\x05temp1\x18\x05 \x01(\x02\x12\r\n\x05temp2\x18\x06 \x01(\x02\x12\x11\n\ttemp_core\x18\x07 \x01(\x02\x12\x10\n\x08utc_time\x18\x08 \x01(\x04\x12\x12\n\nerror_bits\x18\t \x01(\x04\"x\n\x0b\x46\x61\x63toryInfo\x12\n\n\x02\x63X\x18\x01 \x01(\x05\x12\n\n\x02\x63Y\x18\x02 \x01(\x05\x12\n\n\x02\x63Z\x18\x03 \x01(\x05\x12\x14\n\x0cserialNumber\x18\x04 \x01(\r\x12\x17\n\x0fhardwareVersion\x18\x05 \x01(\r\x12\x16\n\x0eyear_month_day\x18\n \x01(\r\"p\n\x07\x43ommand\x12\x14\n\x0creset_device\x18\x01 \x01(\x08\x12\x15\n\rfactory_reset\x18\x02 \x01(\x08\x12&\n\x0bstream_data\x18\x03 \x01(\x0e\x32\x11.bm.FeatureToggle\x12\x10\n\x08\x62oot_now\x18\x04 \x01(\x08\"6\n\x0b\x43\x61libration\x12\'\n\x0b\x63\x61libration\x18\x01 \x01(\x0e\x32\x12.bm.CalibrationCmd\"\xf1\x03\n\x07Request\x12\x13\n\x0bmsg_version\x18\x01 \x01(\r\x12\x31\n\x12set_network_config\x18\x05 \x01(\x0b\x32\x13.bm.NetworkConfigV4H\x00\x12-\n\x11set_sensor_config\x18\x06 \x01(\x0b\x32\x10.bm.SensorConfigH\x00\x12\x39\n\x12get_network_config\x18\x07 \x01(\x0b\x32\x1b.bm.GetNetworkConfigRequestH\x00\x12\x37\n\x11get_sensor_config\x18\x08 \x01(\x0b\x32\x1a.bm.GetSensorConfigRequestH\x00\x12\x33\n\x0fget_sensor_info\x18\t \x01(\x0b\x32\x18.bm.GetSensorInfoRequestH\x00\x12\x1e\n\x07\x63ommand\x18\n \x01(\x0b\x32\x0b.bm.CommandH\x00\x12\x1a\n\x10new_app_password\x18\x14 \x01(\tH\x00\x12\x1e\n\x14new_factory_password\x18\x15 \x01(\tH\x00\x12\'\n\x0b\x63\x61libration\x18\x80\x01 \x01(\x0b\x32\x0f.bm.CalibrationH\x00\x12\x36\n\x10get_factory_info\x18\x81\x01 \x01(\x0b\x32\x19.bm.GetFactoryInfoRequestH\x00\x42\t\n\x07payload\"\x19\n\x17GetNetworkConfigRequest\"\x18\n\x16GetSensorConfigRequest\"\x16\n\x14GetSensorInfoRequest\"\x17\n\x15GetFactoryInfoRequest\"\xe7\x01\n\x08Response\x12\x13\n\x0bmsg_version\x18\x01 \x01(\r\x12\"\n\x06status\x18\x02 \x01(\x0e\x32\x12.bm.ResponseStatus\x12*\n\x0bipv4_config\x18\n \x01(\x0b\x32\x13.bm.NetworkConfigV4H\x00\x12\"\n\x06sensor\x18\x0b \x01(\x0b\x32\x10.bm.SensorConfigH\x00\x12\x1e\n\x04info\x18\x0c \x01(\x0b\x32\x0e.bm.SensorInfoH\x00\x12\'\n\x0c\x66\x61\x63tory_info\x18\r \x01(\x0b\x32\x0f.bm.FactoryInfoH\x00\x42\t\n\x07payload*Q\n\rFeatureToggle\x12\x15\n\x11\x46\x45\x41TURE_UNDEFINED\x10\x00\x12\x14\n\x10\x46\x45\x41TURE_DISABLED\x10\x01\x12\x13\n\x0f\x46\x45\x41TURE_ENABLED\x10\x02*\x88\x01\n\x0e\x41\x63\x63\x65lFullScale\x12\x16\n\x12\x41\x43\x43\x45L_FS_UNDEFINED\x10\x00\x12\x0f\n\x0b\x41\x43\x43\x45L_FS_2G\x10\x01\x12\x0f\n\x0b\x41\x43\x43\x45L_FS_4G\x10\x02\x12\x0f\n\x0b\x41\x43\x43\x45L_FS_8G\x10\x03\x12\x10\n\x0c\x41\x43\x43\x45L_FS_16G\x10\x04\x12\x19\n\x15\x41\x43\x43\x45L_FS_OUT_OF_RANGE\x10\x05*\x8e\x01\n\x08\x41xisMask\x12\x12\n\x0e\x41XIS_UNDEFINED\x10\x00\x12\n\n\x06\x41XIS_X\x10\x01\x12\n\n\x06\x41XIS_Y\x10\x02\x12\n\n\x06\x41XIS_Z\x10\x03\x12\x0b\n\x07\x41XIS_XY\x10\x04\x12\x0b\n\x07\x41XIS_XZ\x10\x05\x12\x0b\n\x07\x41XIS_YZ\x10\x06\x12\x0c\n\x08\x41XIS_XYZ\x10\x07\x12\x15\n\x11\x41XIS_OUT_OF_RANGE\x10\x08*\xc7\x01\n\x06OdrDiv\x12\x15\n\x11ODR_DIV_UNDEFINED\x10\x00\x12\r\n\tODR_DIV_1\x10\x01\x12\r\n\tODR_DIV_2\x10\x02\x12\r\n\tODR_DIV_4\x10\x03\x12\r\n\tODR_DIV_8\x10\x04\x12\x0e\n\nODR_DIV_16\x10\x05\x12\x0e\n\nODR_DIV_32\x10\x06\x12\x0e\n\nODR_DIV_64\x10\x07\x12\x0f\n\x0bODR_DIV_128\x10\x08\x12\x0f\n\x0bODR_DIV_256\x10\t\x12\x18\n\x14ODR_DIV_OUT_OF_RANGE\x10\n*\xdb\x01\n\x0e\x43\x61librationCmd\x12\x19\n\x15\x43\x41LIBRATION_UNDEFINED\x10\x00\x12#\n\x1f\x43\x41LIBRATION_START_XY_ZERO_Z_NEG\x10\x01\x12\x1b\n\x17\x43\x41LIBRATION_START_X_NEG\x10\x02\x12\x1b\n\x17\x43\x41LIBRATION_START_Y_NEG\x10\x03\x12\x1b\n\x17\x43\x41LIBRATION_START_Z_NEG\x10\x04\x12\x14\n\x10\x43\x41LIBRATION_SAVE\x10\x05\x12\x1c\n\x18\x43\x41LIBRATION_OUT_OF_RANGE\x10\x06*\x94\x01\n\rFilterEnabled\x12\x14\n\x10\x46ILTER_UNDEFINED\x10\x00\x12\x0f\n\x0b\x46ILTER_NONE\x10\x01\x12\x14\n\x10\x46ILTER_LOW_PASS2\x10\x02\x12\x14\n\x10\x46ILTER_HIGH_PASS\x10\x03\x12\x17\n\x13\x46ILTER_SLOPE_FILTER\x10\x04\x12\x17\n\x13\x46ILTER_OUT_OF_RANGE\x10\x05*\xf3\x01\n\tCutoffKHz\x12\x14\n\x10\x43UTOFF_UNDEFINED\x10\x00\x12\x13\n\x0f\x43UTOFF_6p66_KHZ\x10\x01\x12\x13\n\x0f\x43UTOFF_2p66_KHZ\x10\x02\x12\x13\n\x0f\x43UTOFF_1p33_KHZ\x10\x03\x12\x13\n\x0f\x43UTOFF_0p59_KHZ\x10\x04\x12\x13\n\x0f\x43UTOFF_0p26_KHZ\x10\x05\x12\x13\n\x0f\x43UTOFF_0p13_KHZ\x10\x06\x12\x13\n\x0f\x43UTOFF_0p06_KHZ\x10\x07\x12\x13\n\x0f\x43UTOFF_0p03_KHZ\x10\x08\x12\x0f\n\x0b\x43UTOFF_NONE\x10\t\x12\x17\n\x13\x43UTOFF_OUT_OF_RANGE\x10\n*\xcc\x04\n\x0eResponseStatus\x12\x1d\n\x19RESPONSE_STATUS_UNDEFINED\x10\x00\x12\x16\n\x12RESPONSE_STATUS_OK\x10\x01\x12!\n\x1dRESPONSE_STATUS_INVALID_PARAM\x10\x02\x12 \n\x1cRESPONSE_STATUS_INVALID_HMAC\x10\x03\x12,\n(RESPONSE_STATUS_INVALID_FULL_SCALE_RANGE\x10\x04\x12\x1f\n\x1bRESPONSE_STATUS_INVALID_ODR\x10\x05\x12%\n!RESPONSE_STATUS_INVALID_AXIS_MASK\x10\x06\x12*\n&RESPONSE_STATUS_INVALID_FILTER_SETTING\x10\x07\x12#\n\x1fRESPONSE_STATUS_INVALID_NETMASK\x10\x08\x12#\n\x1fRESPONSE_STATUS_INVALID_GATEWAY\x10\t\x12(\n$RESPONSE_STATUS_INVALID_NTP_INTERVAL\x10\n\x12&\n\"RESPONSE_STATUS_INVALID_DHCP_STATE\x10\x0b\x12-\n)RESPONSE_STATUS_INVALID_DATA_STREAM_STATE\x10\x0c\x12-\n)RESPONSE_STATUS_INVALID_NTP_MIN_UPDATE_MS\x10\r\x12\"\n\x1dRESPONSE_STATUS_UNKNOWN_ERROR\x10\xff\x01\x62\x06proto3'

# Rename the proto file from 'config/config.proto' to 'sensor/config.proto'.
# Both are 19 ASCII bytes so the varint length prefix (\x13) is unchanged.
DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    _SERIALIZED.replace(b'config/config.proto', b'sensor/config.proto', 1)
)

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'protobuf.sensor_cmd_pb2', _globals)
if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    _globals['_FEATURETOGGLE']._serialized_start = 1955
    _globals['_FEATURETOGGLE']._serialized_end   = 2036
    _globals['_ACCELFULLSCALE']._serialized_start = 2039
    _globals['_ACCELFULLSCALE']._serialized_end   = 2175
    _globals['_AXISMASK']._serialized_start = 2178
    _globals['_AXISMASK']._serialized_end   = 2320
    _globals['_ODRDIV']._serialized_start = 2323
    _globals['_ODRDIV']._serialized_end   = 2522
    _globals['_CALIBRATIONCMD']._serialized_start = 2525
    _globals['_CALIBRATIONCMD']._serialized_end   = 2744
    _globals['_FILTERENABLED']._serialized_start = 2747
    _globals['_FILTERENABLED']._serialized_end   = 2895
    _globals['_CUTOFFKHZ']._serialized_start = 2898
    _globals['_CUTOFFKHZ']._serialized_end   = 3141
    _globals['_RESPONSESTATUS']._serialized_start = 3144
    _globals['_RESPONSESTATUS']._serialized_end   = 3732
    _globals['_NETWORKCONFIGV4']._serialized_start = 28
    _globals['_NETWORKCONFIGV4']._serialized_end   = 377
    _globals['_FILTERCONFIG']._serialized_start = 379
    _globals['_FILTERCONFIG']._serialized_end   = 474
    _globals['_SENSORCONFIG']._serialized_start = 477
    _globals['_SENSORCONFIG']._serialized_end   = 622
    _globals['_SENSORINFO']._serialized_start = 625
    _globals['_SENSORINFO']._serialized_end   = 825
    _globals['_FACTORYINFO']._serialized_start = 827
    _globals['_FACTORYINFO']._serialized_end   = 947
    _globals['_COMMAND']._serialized_start = 949
    _globals['_COMMAND']._serialized_end   = 1061
    _globals['_CALIBRATION']._serialized_start = 1063
    _globals['_CALIBRATION']._serialized_end   = 1117
    _globals['_REQUEST']._serialized_start = 1120
    _globals['_REQUEST']._serialized_end   = 1617
    _globals['_GETNETWORKCONFIGREQUEST']._serialized_start = 1619
    _globals['_GETNETWORKCONFIGREQUEST']._serialized_end   = 1644
    _globals['_GETSENSORCONFIGREQUEST']._serialized_start = 1646
    _globals['_GETSENSORCONFIGREQUEST']._serialized_end   = 1670
    _globals['_GETSENSORINFOREQUEST']._serialized_start = 1672
    _globals['_GETSENSORINFOREQUEST']._serialized_end   = 1694
    _globals['_GETFACTORYINFOREQUEST']._serialized_start = 1696
    _globals['_GETFACTORYINFOREQUEST']._serialized_end   = 1719
    _globals['_RESPONSE']._serialized_start = 1722
    _globals['_RESPONSE']._serialized_end   = 1953
# @@protoc_insertion_point(module_scope)
