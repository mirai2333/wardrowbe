"""Client for the QWeather (和风天气) v7 weather APIs.

The host comes from the QWeather console (an account-specific host such as
``nc3yfvb4mf.re.qweatherapi.com``, or the shared ``devapi.qweather.com``) and the key is sent
as a query parameter, which is what the v7 API expects.

Cold-start access from mainland China is the reason this client exists: the Open-Meteo host
used before times out often enough that the dashboard weather card fails, while QWeather
answers in well under a second from the same network.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OK_CODE = "200"
UNKNOWN_ICON = 999

# QWeather icon codes -> WMO weather codes. Keeping the WMO vocabulary means outfits, the
# recommendation service and the frontend keep speaking the language they already store and
# translate instead of learning a second code system.
ICON_TO_WMO: dict[int, int] = {
    100: 0,
    150: 0,
    101: 3,
    151: 3,
    102: 1,
    152: 1,
    103: 2,
    153: 2,
    104: 3,
    300: 80,
    301: 82,
    302: 95,
    303: 95,
    304: 96,
    305: 61,
    306: 63,
    307: 65,
    308: 65,
    309: 51,
    310: 65,
    311: 65,
    312: 65,
    313: 66,
    314: 63,
    315: 63,
    316: 65,
    317: 65,
    318: 65,
    399: 63,
    400: 71,
    401: 73,
    402: 75,
    403: 75,
    404: 66,
    405: 66,
    406: 66,
    407: 85,
    408: 73,
    409: 75,
    410: 75,
    499: 73,
    500: 45,
    501: 45,
    502: 45,
    503: 45,
    504: 45,
    505: 45,
    506: 45,
    507: 45,
    508: 45,
    509: 45,
    510: 45,
    511: 45,
    512: 45,
    513: 45,
    514: 45,
    515: 45,
    900: 0,
    901: 0,
    999: -1,
}

# QWeather icon codes -> the condition slugs in frontend/messages/<locale>/constants.json under
# constants.weatherConditions. Every slug here already has a translation, so switching provider
# needs no frontend change and no new locale strings.
ICON_TO_CONDITION: dict[int, str] = {
    100: "sunny",
    150: "sunny",
    101: "cloudy",
    151: "cloudy",
    102: "mostly-sunny",
    152: "mostly-sunny",
    103: "partly-cloudy",
    153: "partly-cloudy",
    104: "cloudy",
    300: "showers",
    301: "heavy-showers",
    302: "thunderstorm",
    303: "thunderstorm",
    304: "thunderstorm-with-hail",
    305: "light-rain",
    306: "rain",
    307: "heavy-rain",
    308: "heavy-rain",
    309: "light-drizzle",
    310: "heavy-rain",
    311: "heavy-rain",
    312: "heavy-rain",
    313: "freezing-rain",
    314: "rain",
    315: "rain",
    316: "heavy-rain",
    317: "heavy-rain",
    318: "heavy-rain",
    399: "rain",
    400: "light-snow",
    401: "snow",
    402: "heavy-snow",
    403: "heavy-snow",
    404: "snow",
    405: "snow",
    406: "snow",
    407: "light-snow-showers",
    408: "light-snow",
    409: "snow",
    410: "heavy-snow",
    499: "snow",
    500: "foggy",
    501: "foggy",
    502: "foggy",
    503: "foggy",
    504: "foggy",
    505: "foggy",
    506: "foggy",
    507: "foggy",
    508: "foggy",
    509: "foggy",
    510: "foggy",
    511: "foggy",
    512: "foggy",
    513: "foggy",
    514: "foggy",
    515: "foggy",
    900: "sunny",
    901: "sunny",
    999: "unknown",
}

# QWeather reports precipitation chance only in its hourly payload. Daily forecasts carry the
# expected amount instead, so the daily chance is derived from the icon the same way a person
# reads a forecast: a rain icon means rain is likely, an overcast icon does not.
_ICON_PRECIPITATION_CHANCE: dict[int, int] = {
    300: 60,
    301: 80,
    302: 70,
    303: 80,
    304: 80,
    305: 60,
    306: 70,
    307: 80,
    308: 85,
    309: 55,
    310: 85,
    311: 90,
    312: 90,
    313: 70,
    314: 70,
    315: 75,
    316: 80,
    317: 85,
    318: 90,
    399: 60,
    400: 60,
    401: 65,
    402: 75,
    403: 85,
    404: 60,
    405: 60,
    406: 60,
    407: 55,
    408: 65,
    409: 70,
    410: 80,
    499: 60,
    101: 20,
    151: 20,
    104: 20,
    102: 10,
    152: 10,
    103: 10,
    153: 10,
}


def _icon_code(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return UNKNOWN_ICON


def condition_for_icon(raw: Any) -> str:
    """Map a QWeather icon code to the condition slug the frontend translates."""
    return ICON_TO_CONDITION.get(_icon_code(raw), "unknown")


def wmo_code_for_icon(raw: Any) -> int:
    """Map a QWeather icon code to a WMO code (-1 when the icon is unknown)."""
    return ICON_TO_WMO.get(_icon_code(raw), -1)


def is_night_icon(raw: Any) -> bool:
    """QWeather uses 150-153 for the night variants of its clear/partly-cloudy icons."""
    icon = _icon_code(raw)
    return 150 <= icon < 200


def precipitation_chance_for_icon(raw: Any) -> int:
    return _ICON_PRECIPITATION_CHANCE.get(_icon_code(raw), 0)


class QWeatherClient:
    def __init__(self, api_key: str, api_host: str, timeout: float = 10.0):
        self.api_key = (api_key or "").strip()
        self.api_host = (api_host or "devapi.qweather.com").strip().rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_host)

    async def fetch_now(self, latitude: float, longitude: float) -> dict[str, Any]:
        data = await self._get("/v7/weather/now", self._location_params(latitude, longitude))
        now = data.get("now")
        return now if isinstance(now, dict) else {}

    async def fetch_hourly(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        data = await self._get("/v7/weather/24h", self._location_params(latitude, longitude))
        hourly = data.get("hourly")
        return hourly if isinstance(hourly, list) else []

    async def fetch_daily(
        self, latitude: float, longitude: float, days: int = 7
    ) -> list[dict[str, Any]]:
        # The free plan exposes 3d and 7d (30d needs a paid subscription).
        path = "/v7/weather/3d" if days <= 3 else "/v7/weather/7d"
        data = await self._get(path, self._location_params(latitude, longitude))
        daily = data.get("daily")
        return daily if isinstance(daily, list) else []

    @staticmethod
    def _location_params(latitude: float, longitude: float) -> dict[str, str]:
        # QWeather takes longitude first; the rest of the app passes latitude first.
        return {
            "location": f"{longitude:.4f},{latitude:.4f}",
            "lang": "en",
            "unit": "m",
        }

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.api_key:
            raise QWeatherError("QWeather API key is not configured (set QWEATHER_API_KEY)")

        request_params = {**params, "key": self.api_key}
        url = f"https://{self.api_host}{path}"

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                response = await client.get(url, params=request_params)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"QWeather request to {path} failed: {e}")
                raise QWeatherError(f"QWeather request to {path} failed: {e}") from None
            except ValueError as e:
                logger.error(f"QWeather returned invalid JSON for {path}: {e}")
                raise QWeatherError(
                    f"QWeather returned invalid JSON for {path}: {e}"
                ) from None

        if not isinstance(data, dict):
            raise QWeatherError(f"QWeather returned an unexpected payload for {path}")

        code = str(data.get("code", ""))
        if code != OK_CODE:
            raise QWeatherError(f"QWeather {path} returned status code {code}")

        return data


class QWeatherError(Exception):
    pass
