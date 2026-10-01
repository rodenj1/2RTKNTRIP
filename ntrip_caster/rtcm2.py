"""
RTCM Data Parsing Module (Optimized Version)
Provides RTCM message parsing, STR correction, and real-time data visualization functions.
"""

import socket
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pyproj import Transformer
from pyrtcm import RTCMMessage, RTCMReader, parse_msm

from . import forwarder
from .logger import log_debug, log_error, log_info, log_warning

# Country code mapping table (2-char -> 3-char) - ISO 3166-1
COUNTRY_CODE_MAP = {
    # Asia
    "CN": "CHN",
    "JP": "JPN",
    "KR": "KOR",
    "IN": "IND",
    "ID": "IDN",
    "TH": "THA",
    "VN": "VNM",
    "MY": "MYS",
    "SG": "SGP",
    "PH": "PHL",
    "BD": "BGD",
    "PK": "PAK",
    "LK": "LKA",
    "MM": "MMR",
    "KH": "KHM",
    "LA": "LAO",
    "BN": "BRN",
    "MN": "MNG",
    "KZ": "KAZ",
    "UZ": "UZB",
    "TM": "TKM",
    "KG": "KGZ",
    "TJ": "TJK",
    "AF": "AFG",
    "IR": "IRN",
    "IQ": "IRQ",
    "SY": "SYR",
    "JO": "JOR",
    "LB": "LBN",
    "IL": "ISR",
    "PS": "PSE",
    "SA": "SAU",
    "AE": "ARE",
    "QA": "QAT",
    "BH": "BHR",
    "KW": "KWT",
    "OM": "OMN",
    "YE": "YEM",
    "TR": "TUR",
    "GE": "GEO",
    "AM": "ARM",
    "AZ": "AZE",
    "CY": "CYP",
    "TW": "TWN",
    "HK": "HKG",
    "MO": "MAC",
    "BT": "BTN",
    "MV": "MDV",
    "NP": "NPL",
    "TL": "TLS",
    # Europe
    "GB": "GBR",
    "DE": "DEU",
    "FR": "FRA",
    "IT": "ITA",
    "ES": "ESP",
    "PT": "PRT",
    "NL": "NLD",
    "BE": "BEL",
    "CH": "CHE",
    "AT": "AUT",
    "SE": "SWE",
    "NO": "NOR",
    "DK": "DNK",
    "FI": "FIN",
    "IS": "ISL",
    "IE": "IRL",
    "LU": "LUX",
    "MT": "MLT",
    "PL": "POL",
    "CZ": "CZE",
    "SK": "SVK",
    "HU": "HUN",
    "SI": "SVN",
    "HR": "HRV",
    "BA": "BIH",
    "RS": "SRB",
    "ME": "MNE",
    "MK": "MKD",
    "AL": "ALB",
    "GR": "GRC",
    "BG": "BGR",
    "RO": "ROU",
    "MD": "MDA",
    "UA": "UKR",
    "BY": "BLR",
    "LT": "LTU",
    "LV": "LVA",
    "EE": "EST",
    "RU": "RUS",
    "AD": "AND",
    "MC": "MCO",
    "SM": "SMR",
    "VA": "VAT",
    "LI": "LIE",
    # North America
    "US": "USA",
    "CA": "CAN",
    "MX": "MEX",
    "GT": "GTM",
    "BZ": "BLZ",
    "SV": "SLV",
    "HN": "HND",
    "NI": "NIC",
    "CR": "CRI",
    "PA": "PAN",
    "CU": "CUB",
    "JM": "JAM",
    "HT": "HTI",
    "DO": "DOM",
    "TT": "TTO",
    "BB": "BRB",
    "GD": "GRD",
    "VC": "VCT",
    "LC": "LCA",
    "DM": "DMA",
    "AG": "ATG",
    "KN": "KNA",
    "BS": "BHS",
    # South America
    "BR": "BRA",
    "AR": "ARG",
    "CL": "CHL",
    "PE": "PER",
    "CO": "COL",
    "VE": "VEN",
    "EC": "ECU",
    "BO": "BOL",
    "PY": "PRY",
    "UY": "URY",
    "GY": "GUY",
    "SR": "SUR",
    "GF": "GUF",
    "FK": "FLK",
    # Africa
    "ZA": "ZAF",
    "EG": "EGY",
    "NG": "NGA",
    "KE": "KEN",
    "ET": "ETH",
    "GH": "GHA",
    "UG": "UGA",
    "TZ": "TZA",
    "MZ": "MOZ",
    "MG": "MDG",
    "CM": "CMR",
    "CI": "CIV",
    "NE": "NER",
    "BF": "BFA",
    "ML": "MLI",
    "MW": "MWI",
    "ZM": "ZMB",
    "ZW": "ZWE",
    "BW": "BWA",
    "NA": "NAM",
    "SZ": "SWZ",
    "LS": "LSO",
    "MU": "MUS",
    "SC": "SYC",
    "MR": "MRT",
    "SN": "SEN",
    "GM": "GMB",
    "GW": "GNB",
    "GN": "GIN",
    "SL": "SLE",
    "LR": "LBR",
    "TG": "TGO",
    "BJ": "BEN",
    "CV": "CPV",
    "ST": "STP",
    "GQ": "GNQ",
    "GA": "GAB",
    "CG": "COG",
    "CD": "COD",
    "CF": "CAF",
    "TD": "TCD",
    "LY": "LBY",
    "TN": "TUN",
    "DZ": "DZA",
    "MA": "MAR",
    "EH": "ESH",
    "SD": "SDN",
    "SS": "SSD",
    "ER": "ERI",
    "DJ": "DJI",
    "SO": "SOM",
    "RW": "RWA",
    "BI": "BDI",
    "KM": "COM",
    "AO": "AGO",
    # Oceania
    "AU": "AUS",
    "NZ": "NZL",
    "FJ": "FJI",
    "PG": "PNG",
    "SB": "SLB",
    "NC": "NCL",
    "PF": "PYF",
    "VU": "VUT",
    "WS": "WSM",
    "TO": "TON",
    "TV": "TUV",
    "KI": "KIR",
    "NR": "NRU",
    "PW": "PLW",
    "FM": "FSM",
    "MH": "MHL",
    "CK": "COK",
    "NU": "NIU",
    "TK": "TKL",
    "WF": "WLF",
    "AS": "ASM",
    "GU": "GUM",
    "MP": "MNP",
    # Antarctica
    "AQ": "ATA",
}

# RTCM message type and carrier mapping (including constellation info)
CARRIER_INFO = {
    # GPS (1070-1077)
    (1070, 1070): ("GPS", "L1"),
    (1071, 1071): ("GPS", "L1+L2"),
    (1072, 1072): ("GPS", "L2"),
    (1073, 1073): ("GPS", "L1+C1"),
    (1074, 1074): ("GPS", "L5"),
    (1075, 1075): ("GPS", "L1+L5"),
    (1076, 1076): ("GPS", "L2+L5"),
    (1077, 1077): ("GPS", "L1+L2+L5"),
    # GLONASS (1080-1087)
    (1080, 1080): ("GLO", "G1"),
    (1081, 1081): ("GLO", "G1+G2"),
    (1082, 1082): ("GLO", "G2"),
    (1083, 1083): ("GLO", "G1+C1"),
    (1084, 1084): ("GLO", "G3"),
    (1085, 1085): ("GLO", "G1+G3"),
    (1086, 1086): ("GLO", "G2+G3"),
    (1087, 1087): ("GLO", "G1+G2+G3"),
    # Galileo (1090-1097)
    (1090, 1090): ("GAL", "E1"),
    (1091, 1091): ("GAL", "E1+E5b"),
    (1092, 1092): ("GAL", "E5b"),
    (1093, 1093): ("GAL", "E1+C1"),
    (1094, 1094): ("GAL", "E5a"),
    (1095, 1095): ("GAL", "E1+E5a"),
    (1096, 1096): ("GAL", "E5b+E5a"),
    (1097, 1097): ("GAL", "E1+E5a+E5b"),
    # QZSS (1100-1107)
    (1100, 1100): ("QZSS", "L1"),
    (1101, 1101): ("QZSS", "L1+L2"),
    (1102, 1102): ("QZSS", "L2"),
    (1103, 1103): ("QZSS", "L1+C1"),
    (1104, 1104): ("QZSS", "L5"),
    (1105, 1105): ("QZSS", "L1+L5"),
    (1106, 1106): ("QZSS", "L2+L5"),
    (1107, 1107): ("QZSS", "L1+L2+L5+LEX"),
    # IRNSS (1110-1117)
    (1110, 1110): ("IRNSS", "L5"),
    (1111, 1111): ("IRNSS", "L5+S"),
    (1112, 1112): ("IRNSS", "S"),
    (1113, 1113): ("IRNSS", "L5+C1"),
    (1114, 1114): ("IRNSS", "L1"),
    (1115, 1115): ("IRNSS", "L1+L5"),
    (1116, 1116): ("IRNSS", "L1+S"),
    (1117, 1117): ("IRNSS", "L1+L5+S"),
    # BDS (1120-1127)
    (1120, 1120): ("BDS", "B1I"),
    (1121, 1121): ("BDS", "B1I+B3I"),
    (1122, 1122): ("BDS", "B3I"),
    (1123, 1123): ("BDS", "B1I+B2I"),
    (1124, 1124): ("BDS", "B2I"),
    (1125, 1125): ("BDS", "B1I+B2I"),
    (1126, 1126): ("BDS", "B2I+B3I"),
    (1127, 1127): ("BDS", "B1I+B2I+B3I"),
    # SBAS (1040-1047)
    (1040, 1040): ("SBAS", "L1"),
    (1041, 1041): ("SBAS", "L1+L5"),
    (1042, 1042): ("SBAS", "L5"),
    (1043, 1043): ("SBAS", "L1+C1"),
    (1044, 1044): ("SBAS", "L1+L2"),
    (1045, 1045): ("SBAS", "L2+L5"),
    (1046, 1046): ("SBAS", "L2"),
    (1047, 1047): ("SBAS", "L1+L2+L5"),
}


class DataType:
    MSM_SATELLITE = "msm_satellite"  # MSM satellite signal data
    GEOGRAPHY = "geography"  # Geographic position data
    DEVICE_INFO = "device_info"  # Device information
    BITRATE = "bitrate"  # Bitrate data
    MESSAGE_STATS = "message_stats"  # Message statistics


class RTCMParserThread(threading.Thread):
    """RTCM Data Parsing Thread"""

    def __init__(
        self,
        mount_name: str,
        mode: str = "str_fix",
        duration: int = 30,
        push_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.mount_name = mount_name
        self.mode = mode  # str_fix or realtime_web
        self.duration = duration
        self.push_callback = push_callback

        self.running = threading.Event()
        self.running.set()

        self.result: dict[str, Any] = {
            "mount": mount_name,
            "location": None,
            "device": None,
            "bitrate": None,
            "message_stats": {
                "types": defaultdict(int),
                "gnss": set(),
                "carriers": set(),
                "frequency": {},
            },
        }
        self.result_lock = threading.Lock()

        self.pipe_r, self.pipe_w = socket.socketpair()
        self.pipe_r.settimeout(5.0)

        self.stats_start_time = time.time()
        self.total_bytes = 0
        self.last_stats_time = time.time()
        self.stats_delay = 5.0
        self.stats_enabled = False

        log_debug(f"RTCMParserThread initialized [Mount: {mount_name}, Mode: {mode}]")

    def run(self) -> None:
        """Thread main logic"""
        log_info(f"Starting parsing thread [Mount: {self.mount_name}, Mode: {self.mode}]")
        try:
            forwarder.register_subscriber(self.mount_name, self.pipe_w)
            stream = self.pipe_r.makefile("rb")
            reader = RTCMReader(stream)
            self.start_time = time.time()

            while self.running.is_set():
                if self.mode == "str_fix" and time.time() - self.start_time > self.duration:
                    log_info(f"RTCM parsing thread completed [Mount: {self.mount_name}, Duration: {self.duration}s]")
                    break

                try:
                    raw, msg = next(reader)
                    if not msg:
                        continue

                    current_time = time.time()
                    if not self.stats_enabled and current_time - self.start_time >= self.stats_delay:
                        self.stats_enabled = True
                        self.stats_start_time = current_time
                        self.last_stats_time = current_time
                        self.total_bytes = 0
                        log_info(
                            f"Starting bitrate statistics [Mount: {self.mount_name}] - "
                            f"Enabled after {self.stats_delay}s delay"
                        )

                    if self.stats_enabled:
                        self.total_bytes += len(raw)

                    msg_id = self._get_msg_id(msg)
                    if msg_id:
                        self._update_message_stats(msg_id)
                        if self.mode == "str_fix":
                            self._process_str_fix(msg, msg_id, raw)
                        else:  # realtime_web
                            self._process_realtime_web(msg, msg_id, raw)

                    if self.stats_enabled and time.time() - self.last_stats_time >= 10:
                        self._calculate_bitrate()
                        self._calculate_message_frequency()
                        self._generate_gnss_carrier_info()

                except StopIteration:
                    break
                except TimeoutError:
                    continue
                except Exception as e:
                    error_msg = str(e)
                    if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                        continue
                    else:
                        log_error(f"Message parsing error [Mount: {self.mount_name}]: {error_msg}")

        except Exception as e:
            log_error(f"Parsing thread exception [Mount: {self.mount_name}]: {e!s}")
        finally:
            forwarder.unregister_subscriber(self.mount_name, self.pipe_w)
            self.pipe_r.close()
            self.pipe_w.close()
            log_info(f"Parsing thread stopped [Mount: {self.mount_name}]")

    def _get_msg_id(self, msg: RTCMMessage) -> int | None:
        """Get message ID safely"""
        try:
            return int(getattr(msg, "identity", -1))
        except (ValueError, TypeError):
            return None

    def _process_location_message(self, msg: RTCMMessage, msg_id: int) -> None:
        """Process 1005/1006 messages, extract position and station ID"""
        if msg_id not in (1005, 1006):
            return

        station_id = getattr(msg, "DF003", None) if hasattr(msg, "DF003") else None

        try:
            x, y, z = msg.DF025, msg.DF026, msg.DF027
            transformer = Transformer.from_crs("epsg:4978", "epsg:4326", always_xy=True)
            lon, lat, height = transformer.transform(x, y, z)

            country_code, country_name, city = self._reverse_geocode(lat, lon)
            country_3code = COUNTRY_CODE_MAP.get(country_code, country_code) if country_code else None

            location_data = {
                "mount": self.mount_name,
                "mount_name": self.mount_name,
                "station_id": station_id,
                "id": station_id,
                "name": self.mount_name,
                "ecef": {"x": x, "y": y, "z": z},
                "x": x,
                "y": y,
                "z": z,
                "lat": round(lat, 8),
                "latitude": round(lat, 8),
                "lon": round(lon, 8),
                "longitude": round(lon, 8),
                "height": round(height, 3),
                "country": country_3code,
                "country_code": country_code,
                "country_name": country_name,
                "city": city,
            }

            with self.result_lock:
                self.result["location"] = location_data
            self._push_data(DataType.GEOGRAPHY, location_data)

        except Exception as e:
            log_error(f"Location parsing error: {e!s}")

    def _reverse_geocode(
        self, lat: float, lon: float, min_population: int = 10000
    ) -> tuple[str | None, str | None, str | None]:
        """Coordinates reverse search for country and city"""
        try:
            import reverse_geocode

            result = reverse_geocode.get((lat, lon), min_population=min_population)
            if not result:
                return None, None, None
            return result.get("country_code"), result.get("country"), result.get("city")
        except ImportError:
            log_warning("reverse_geocode library not installed")
            return None, None, None
        except Exception as e:
            log_warning(f"Geocoding failed: {e!s}")
            return None, None, None

    def _process_device_info(self, msg: RTCMMessage, msg_id: int) -> None:
        """Process 1033 message, extract device info"""
        if msg_id != 1033:
            return

        try:
            antenna_parts = []
            for i in range(1, 21):
                field_name = f"DF030_{i:02d}"
                if hasattr(msg, field_name):
                    part = getattr(msg, field_name)
                    if part and part != 0:
                        antenna_parts.append(chr(part) if isinstance(part, int) and 0 < part < 256 else str(part))
            antenna = "".join(antenna_parts).strip() if antenna_parts else None

            receiver_parts = []
            for i in range(1, 31):
                field_name = f"DF228_{i:02d}"
                if hasattr(msg, field_name):
                    part = getattr(msg, field_name)
                    if part and part != 0:
                        receiver_parts.append(chr(part) if isinstance(part, int) and 0 < part < 256 else str(part))
            receiver = "".join(receiver_parts).strip() if receiver_parts else None

            firmware_parts = []
            for i in range(1, 21):
                field_name = f"DF230_{i:02d}"
                if hasattr(msg, field_name):
                    part = getattr(msg, field_name)
                    if part and part != 0:
                        firmware_parts.append(chr(part) if isinstance(part, int) and 0 < part < 256 else str(part))
            firmware = "".join(firmware_parts).strip() if firmware_parts else None

            antenna_serial = getattr(msg, "DF033", None) or getattr(msg, "DF032", None)

            device_data = {
                "mount": self.mount_name,
                "receiver": receiver,
                "firmware": firmware,
                "antenna": antenna,
                "antenna_firmware": antenna_serial,
            }

            with self.result_lock:
                self.result["device"] = device_data
            self._push_data(DataType.DEVICE_INFO, device_data)

        except Exception as e:
            log_error(f"Device info parsing error: {e!s}")

    def _calculate_bitrate(self) -> None:
        """Calculate bitrate"""
        if not self.stats_enabled:
            return

        current_time = time.time()
        elapsed = current_time - self.last_stats_time
        if elapsed < 1:
            return

        bitrate = (self.total_bytes * 8) / elapsed

        with self.result_lock:
            self.result["bitrate"] = round(bitrate, 2)

        self._push_data(
            DataType.BITRATE, {"mount": self.mount_name, "bitrate": round(bitrate, 2), "period": f"{elapsed:.1f}s"}
        )

        self.total_bytes = 0
        self.last_stats_time = current_time

    def _update_message_stats(self, msg_id: int) -> None:
        """Update message type count, constellation and carrier info"""
        with self.result_lock:
            self.result["message_stats"]["types"][msg_id] += 1
            for (start, end), (gnss, carrier) in CARRIER_INFO.items():
                if start <= msg_id <= end:
                    self.result["message_stats"]["gnss"].add(gnss)
                    for c in carrier.split("+"):
                        self.result["message_stats"]["carriers"].add(c)
                    break

    def _calculate_message_frequency(self) -> None:
        """Calculate message type frequency over 10s"""
        with self.result_lock:
            types = self.result["message_stats"]["types"]
            frequency = {msg_id: max(1, round(count / 10)) for msg_id, count in types.items()}
            self.result["message_stats"]["frequency"] = frequency

    def _generate_gnss_carrier_info(self) -> None:
        """Generate constellation and carrier strings and push"""
        with self.result_lock:
            gnss_str = "+".join(sorted(self.result["message_stats"]["gnss"])) or "N/A"
            carrier_str = "+".join(sorted(self.result["message_stats"]["carriers"])) or "N/A"
            types_str = ",".join([f"{k}({v})" for k, v in self.result["message_stats"]["frequency"].items()])

            stats_data = {
                "mount": self.mount_name,
                "message_types": types_str,
                "gnss": gnss_str,
                "carriers": carrier_str,
            }

        self._push_data(DataType.MESSAGE_STATS, stats_data)

    def _process_msm_messages(self, msg: RTCMMessage, msg_id: int) -> None:
        """Process MSM messages, extract signal strength"""
        if not (1040 <= msg_id <= 1127):
            return

        try:
            msm_result = parse_msm(msg)
            if not msm_result:
                return

            meta, _, msmcells = msm_result
            if not msmcells:
                return

            sats_data = []
            for cell in msmcells:
                cnr = cell.get("DF408") or cell.get("DF403") or cell.get("DF405") or 0
                if cnr > 0:
                    sat_data = {
                        "id": cell.get("CELLPRN", 0),
                        "signal_type": cell.get("CELLSIG", 0),
                        "snr": cnr,
                        "lock_time": cell.get("DF407", 0),
                        "pseudorange": cell.get("DF400", 0),
                        "carrier_phase": cell.get("DF401", 0) or cell.get("DF406", 0),
                        "doppler": cell.get("DF404", 0),
                    }
                    sats_data.append(sat_data)

            if sats_data:
                self._push_data(
                    DataType.MSM_SATELLITE,
                    {
                        "gnss": meta.get("gnss", "UNKNOWN"),
                        "msg_type": msg_id,
                        "station_id": meta.get("station", 0),
                        "epoch": meta.get("epoch", 0),
                        "total_sats": len(sats_data),
                        "sats": sats_data,
                    },
                )

        except Exception as e:
            log_debug(f"MSM parsing skipped: {e!s}")

    def _process_str_fix(self, msg: RTCMMessage, msg_id: int, raw: bytes) -> None:
        """STR fix mode logic"""
        if msg_id in (1005, 1006):
            self._process_location_message(msg, msg_id)
        elif msg_id == 1033:
            self._process_device_info(msg, msg_id)

    def _process_realtime_web(self, msg: RTCMMessage, msg_id: int, raw: bytes) -> None:
        """Web real-time mode logic"""
        if msg_id in (1005, 1006):
            self._process_location_message(msg, msg_id)
        elif msg_id == 1033:
            self._process_device_info(msg, msg_id)
        elif msg_id in range(1070, 1130):
            self._process_msm_messages(msg, msg_id)
        else:
            self._process_location_message(msg, msg_id)
            self._process_device_info(msg, msg_id)
            self._process_msm_messages(msg, msg_id)

    def _push_data(self, data_type: str, data: dict[str, Any]) -> None:
        """Push data via callback"""
        if self.push_callback:
            try:
                self.push_callback(
                    {"mount_name": self.mount_name, "data_type": data_type, "timestamp": time.time(), **data}
                )
            except Exception as e:
                log_error(f"Data push failed: {e!s}")

    def stop(self) -> None:
        """Stop parsing thread"""
        self.running.clear()
        # Wake a read that is waiting for data: shutting the pipe's write side gives the
        # reader end-of-stream at once, so the thread exits now rather than after its
        # read timeout. The forwarder drops this subscriber on its next write.
        try:
            self.pipe_w.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.join(timeout=5)
        log_info(f"Parsing thread closed for mount {self.mount_name}")


def start_str_fix_parser(
    mount_name: str, duration: int = 30, callback: Callable[[dict[str, Any]], None] | None = None
) -> RTCMParserThread:
    """Start parsing thread in STR fix mode"""
    parser = RTCMParserThread(mount_name, mode="str_fix", duration=duration, push_callback=callback)
    parser.start()
    return parser


def start_web_parser(mount_name: str, callback: Callable[[dict[str, Any]], None] | None = None) -> RTCMParserThread:
    """Start parsing thread in Web real-time mode"""
    parser = RTCMParserThread(mount_name, mode="realtime_web", push_callback=callback)
    parser.start()
    return parser
