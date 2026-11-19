import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import redis.asyncio as aioredis

from app.config import get_settings
from app.services.qweather_client import (
    QWeatherClient,
    QWeatherError,
    condition_for_icon,
    is_night_icon,
    precipitation_chance_for_icon,
    wmo_code_for_icon,
)
from app.utils.redis_lock import get_redis

logger = logging.getLogger(__name__)

WEARING_DAY_END_HOUR = 21
MIN_WINDOW_HOURS = 2
settings = get_settings()


@dataclass
class WeatherData:
    temperature: float  # Celsius
    feels_like: float  # Apparent temperature
    humidity: int  # Percentage
    precipitation_chance: int  # Percentage
    precipitation_mm: float  # mm in next hour
    wind_speed: float  # km/h
    condition: str  # sunny, cloudy, rainy, snowy, etc.
    condition_code: int  # WMO weather code
    is_day: bool
    uv_index: float
    timestamp: datetime
    temp_min: float | None = None  # Today's forecast low, Celsius
    temp_max: float | None = None  # Today's forecast high, Celsius
    # Range over the remaining wearing window (current hour through WEARING_DAY_END_HOUR local,
    # or the whole day for a day-ahead forecast). None when fewer than MIN_WINDOW_HOURS remain,
    # so that late-evening requests score against the current reading only.
    window_min: float | None = None
    window_max: float | None = None

    def to_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "feels_like": self.feels_like,
            "humidity": self.humidity,
            "precipitation_chance": self.precipitation_chance,
            "precipitation_mm": self.precipitation_mm,
            "wind_speed": self.wind_speed,
            "condition": self.condition,
            "condition_code": self.condition_code,
            "is_day": self.is_day,
            "uv_index": self.uv_index,
            "timestamp": self.timestamp.isoformat(),
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
            "window_min": self.window_min,
            "window_max": self.window_max,
        }


@dataclass
class DailyForecast:
    date: str  # YYYY-MM-DD
    temp_min: float
    temp_max: float
    precipitation_chance: int
    condition: str
    condition_code: int
    humidity: int | None = None
    wind_speed: float | None = None
    uv_index: float | None = None


# QWeather's free plan has a small daily quota, and the phone setup this deployment targets has
# no Redis (REDIS_URL points at 127.0.0.1:6379), which would otherwise mean three upstream calls
# on every dashboard render. The in-process cache is a fallback only and is never consulted for
# open-meteo, so the provider switch does not change the existing cache tests.
QWEATHER_MEMORY_CACHE_TTL = 600.0
_QWEATHER_MEMORY_CACHE: dict[str, tuple[float, WeatherData]] = {}


# WMO Weather interpretation codes
# https://open-meteo.com/en/docs
WMO_CODES = {
    0: "sunny",
    1: "mostly sunny",
    2: "partly cloudy",
    3: "cloudy",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


CACHE_TTL = 3600  # 1 hour
CACHE_PREFIX = "weather:"
QWEATHER_CACHE_PREFIX = "weather:qweather:"

GEOCODE_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days; geocoding a place name rarely changes
GEOCODE_NEGATIVE_CACHE_TTL = 3600  # 1 hour for not-found results; avoids hammering Nominatim
GEOCODE_CACHE_PREFIX = "geocode:"


# QWeather returns every numeric field as a string, and an occasional field is missing. These
# helpers keep the conversion noise out of the assembly code below.
def _to_float(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _to_float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _to_int(value: object, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _to_int_or_none(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


class WeatherService:
    def __init__(self):
        self.base_url = settings.openmeteo_url
        self.provider = (settings.weather_provider or "open-meteo").strip().lower()
        self.qweather = (
            QWeatherClient(
                api_key=settings.qweather_api_key or "",
                api_host=settings.qweather_api_host,
            )
            if self.provider == "qweather"
            else None
        )

    async def geocode_location_name(
        self, location_name: str
    ) -> tuple[float, float, str | None] | None:
        query = location_name.strip()
        if not query:
            return None

        cached = await self._geocode_cache_get(query)
        if cached is not None:
            if not cached:
                return None
            return cached

        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
        }

        headers = {
            "User-Agent": settings.get_geocoding_user_agent(),
        }

        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, headers=headers
        ) as client:
            try:
                response = await client.get(
                    "https://nominatim.openstreetmap.org/search", params=params
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"Geocoding error for {query!r}: {e}")
                raise GeocodingServiceError(f"Failed to geocode location {query!r}: {e}") from None
            except ValueError as e:
                logger.error(f"Geocoding returned invalid JSON for {query!r}: {e}")
                raise GeocodingServiceError(
                    f"Failed to decode geocoding response for location {query!r}: {e}"
                ) from None

        if not data or not isinstance(data, list):
            await self._geocode_cache_set_miss(query)
            return None

        first = data[0]
        try:
            latitude = float(first["lat"])
            longitude = float(first["lon"])
        except (KeyError, TypeError, ValueError):
            return None

        display_name = first.get("display_name")
        result = (latitude, longitude, display_name)
        await self._geocode_cache_set(query, result)
        return result

    @staticmethod
    def _geocode_cache_key(query: str) -> str:
        return f"{GEOCODE_CACHE_PREFIX}{query.strip().lower()}"

    async def _geocode_cache_get(
        self, query: str
    ) -> tuple[float, float, str | None] | tuple[()] | None:
        try:
            redis = await get_redis()
            raw = await redis.get(self._geocode_cache_key(query))
        except aioredis.RedisError:
            logger.debug(f"Redis unavailable for geocode cache read ({query!r})")
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            logger.debug(f"Geocode cache value for {query!r} could not be decoded; discarding")
            return None
        if data is None:
            return ()
        return data["lat"], data["lon"], data.get("display_name")

    async def _geocode_cache_set_miss(self, query: str) -> None:
        try:
            redis = await get_redis()
            await redis.set(
                self._geocode_cache_key(query),
                json.dumps(None),
                ex=GEOCODE_NEGATIVE_CACHE_TTL,
            )
        except aioredis.RedisError:
            logger.debug(f"Redis unavailable for geocode cache write ({query!r})")

    async def _geocode_cache_set(self, query: str, result: tuple[float, float, str | None]) -> None:
        lat, lon, display_name = result
        try:
            redis = await get_redis()
            await redis.set(
                self._geocode_cache_key(query),
                json.dumps({"lat": lat, "lon": lon, "display_name": display_name}),
                ex=GEOCODE_CACHE_TTL,
            )
        except aioredis.RedisError:
            logger.debug(f"Redis unavailable for geocode cache write ({query!r})")

    @staticmethod
    def _cache_key(lat: float, lon: float, provider: str = "open-meteo") -> str:
        prefix = QWEATHER_CACHE_PREFIX if provider == "qweather" else CACHE_PREFIX
        return f"{prefix}{round(lat, 2)},{round(lon, 2)}"

    async def _cache_get(self, lat: float, lon: float) -> WeatherData | None:
        try:
            redis = await get_redis()
            raw = await redis.get(self._cache_key(lat, lon, self.provider))
        except aioredis.RedisError:
            logger.debug(f"Redis unavailable for weather cache read ({lat}, {lon})")
            return None
        if raw is None:
            return None
        data = json.loads(raw)
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        logger.debug(f"Weather cache hit for ({lat}, {lon})")
        return WeatherData(**data)

    async def _cache_set(self, lat: float, lon: float, data: WeatherData) -> None:
        try:
            redis = await get_redis()
            await redis.set(
                self._cache_key(lat, lon, self.provider),
                json.dumps(data.to_dict()),
                ex=CACHE_TTL,
            )
        except aioredis.RedisError:
            logger.debug(f"Redis unavailable for weather cache write ({lat}, {lon})")

    def _validate_coordinates(self, latitude: float, longitude: float) -> None:
        """Validate latitude and longitude bounds."""
        if not -90 <= latitude <= 90:
            raise ValueError(f"Invalid latitude {latitude}: must be between -90 and 90")
        if not -180 <= longitude <= 180:
            raise ValueError(f"Invalid longitude {longitude}: must be between -180 and 180")

    def _interpret_weather_code(self, code: int) -> str:
        """Convert WMO weather code to human-readable condition."""
        return WMO_CODES.get(code, "unknown")

    async def get_current_weather(
        self, latitude: float, longitude: float, use_cache: bool = True
    ) -> WeatherData:
        """
        Fetch current weather for a location.

        Args:
            latitude: Location latitude
            longitude: Location longitude
            use_cache: Whether to use cached data if available

        Returns:
            WeatherData with current conditions

        Raises:
            ValueError: If coordinates are out of bounds
            WeatherServiceError: If API request fails
        """
        self._validate_coordinates(latitude, longitude)

        if self.provider == "qweather":
            return await self._qweather_current_weather(latitude, longitude, use_cache)

        if use_cache:
            cached = await self._cache_get(latitude, longitude)
            if cached:
                return cached

        # Fetch from API
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": [
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "is_day",
                "uv_index",
            ],
            "hourly": ["precipitation_probability", "temperature_2m"],
            "daily": ["temperature_2m_max", "temperature_2m_min"],
            "forecast_hours": 24,
            "forecast_days": 1,
            "timezone": "auto",
        }

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                response = await client.get(f"{self.base_url}/forecast", params=params)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"Weather API error: {e}")
                raise WeatherServiceError(f"Failed to fetch weather: {e}") from None

        current = data.get("current", {})
        hourly = data.get("hourly", {})
        daily = data.get("daily") or {}

        precip_probs = hourly.get("precipitation_probability", [])
        precip_chance = precip_probs[0] if precip_probs else 0

        weather_code = current.get("weather_code", 0)

        temp_maxs = daily.get("temperature_2m_max") or []
        temp_mins = daily.get("temperature_2m_min") or []
        temp_max = temp_maxs[0] if temp_maxs and temp_maxs[0] is not None else None
        temp_min = temp_mins[0] if temp_mins and temp_mins[0] is not None else None
        window = self._wearing_window(current, hourly)

        weather = WeatherData(
            temperature=current.get("temperature_2m", 0),
            feels_like=current.get("apparent_temperature", 0),
            humidity=current.get("relative_humidity_2m", 0),
            precipitation_chance=precip_chance,
            precipitation_mm=current.get("precipitation", 0),
            wind_speed=current.get("wind_speed_10m", 0),
            condition=self._interpret_weather_code(weather_code),
            condition_code=weather_code,
            is_day=bool(current.get("is_day", 1)),
            uv_index=current.get("uv_index", 0),
            timestamp=datetime.utcnow(),
            temp_min=temp_min,
            temp_max=temp_max,
            window_min=window[0] if window else None,
            window_max=window[1] if window else None,
        )

        await self._cache_set(latitude, longitude, weather)

        logger.info(
            f"Weather fetched for ({latitude}, {longitude}): "
            f"{weather.temperature}°C (day {weather.temp_min}-{weather.temp_max}°C, "
            f"ahead {weather.window_min}-{weather.window_max}°C), {weather.condition}"
        )

        return weather

    async def _qweather_current_weather(
        self, latitude: float, longitude: float, use_cache: bool = True
    ) -> WeatherData:
        """Current conditions from QWeather, assembled into the same WeatherData shape."""
        self._validate_coordinates(latitude, longitude)

        if use_cache:
            cached = await self._cache_get(latitude, longitude)
            if cached:
                return cached
            memory = self._qweather_memory_get(latitude, longitude)
            if memory:
                return memory

        if self.qweather is None:
            raise WeatherServiceError("QWeather provider is not configured")

        try:
            now = await self.qweather.fetch_now(latitude, longitude)
        except QWeatherError as e:
            raise WeatherServiceError(f"Failed to fetch QWeather current weather: {e}") from None

        hourly: list[dict] = []
        try:
            hourly = await self.qweather.fetch_hourly(latitude, longitude)
        except QWeatherError as e:
            logger.warning(
                f"QWeather hourly data unavailable for ({latitude}, {longitude}): {e}"
            )

        daily: list[dict] = []
        try:
            daily = await self.qweather.fetch_daily(latitude, longitude, days=1)
        except QWeatherError as e:
            logger.warning(
                f"QWeather daily data unavailable for ({latitude}, {longitude}): {e}"
            )

        today = daily[0] if daily else {}
        temperature = _to_float(now.get("temp"), 0.0)
        icon = now.get("icon")

        weather = WeatherData(
            temperature=temperature,
            feels_like=_to_float(now.get("feelsLike"), temperature),
            humidity=_to_int(now.get("humidity"), 0),
            precipitation_chance=_to_int(hourly[0].get("pop"), 0) if hourly else 0,
            precipitation_mm=_to_float(now.get("precip"), 0.0),
            wind_speed=_to_float(now.get("windSpeed"), 0.0),
            condition=condition_for_icon(icon),
            condition_code=wmo_code_for_icon(icon),
            is_day=not is_night_icon(icon),
            uv_index=_to_float(today.get("uvIndex"), 0.0),
            timestamp=datetime.utcnow(),
            temp_min=_to_float_or_none(today.get("tempMin")),
            temp_max=_to_float_or_none(today.get("tempMax")),
        )

        window = self._qweather_wearing_window(now, hourly)
        if window:
            weather.window_min, weather.window_max = window

        await self._cache_set(latitude, longitude, weather)
        self._qweather_memory_set(latitude, longitude, weather)

        logger.info(
            f"QWeather fetched for ({latitude}, {longitude}): "
            f"{weather.temperature}°C (day {weather.temp_min}-{weather.temp_max}°C, "
            f"ahead {weather.window_min}-{weather.window_max}°C), {weather.condition}"
        )

        return weather

    @staticmethod
    def _qweather_wearing_window(
        now: dict, hourly: list[dict]
    ) -> tuple[float, float] | None:
        """Temperature range from now through the evening, mirroring _wearing_window."""
        if not hourly:
            return None
        try:
            reference = datetime.fromisoformat(str(now.get("obsTime")))
        except ValueError:
            return None

        temperatures: list[float] = []
        for slot in hourly:
            try:
                slot_time = datetime.fromisoformat(str(slot.get("fxTime")))
            except ValueError:
                continue
            if slot_time.date() != reference.date() or slot_time.hour > WEARING_DAY_END_HOUR:
                continue
            temp = _to_float_or_none(slot.get("temp"))
            if temp is not None:
                temperatures.append(temp)

        if len(temperatures) < MIN_WINDOW_HOURS:
            return None

        current = _to_float_or_none(now.get("temp"))
        if current is not None:
            temperatures.append(current)
        return min(temperatures), max(temperatures)

    @staticmethod
    def _qweather_memory_get(lat: float, lon: float) -> WeatherData | None:
        key = WeatherService._cache_key(lat, lon, "qweather")
        entry = _QWEATHER_MEMORY_CACHE.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() >= expires_at:
            _QWEATHER_MEMORY_CACHE.pop(key, None)
            return None
        return data

    @staticmethod
    def _qweather_memory_set(lat: float, lon: float, data: WeatherData) -> None:
        key = WeatherService._cache_key(lat, lon, "qweather")
        _QWEATHER_MEMORY_CACHE[key] = (
            time.monotonic() + QWEATHER_MEMORY_CACHE_TTL,
            data,
        )

    async def _qweather_daily_forecast(
        self, latitude: float, longitude: float, days: int
    ) -> list[DailyForecast]:
        if self.qweather is None:
            raise WeatherServiceError("QWeather provider is not configured")

        try:
            raw = await self.qweather.fetch_daily(latitude, longitude, days)
        except QWeatherError as e:
            raise WeatherServiceError(f"Failed to fetch QWeather forecast: {e}") from None

        forecasts: list[DailyForecast] = []
        for day in raw[:days]:
            date = str(day.get("fxDate") or "")
            if not date:
                continue
            icon = day.get("iconDay")
            forecasts.append(
                DailyForecast(
                    date=date,
                    temp_max=_to_float(day.get("tempMax"), 0.0),
                    temp_min=_to_float(day.get("tempMin"), 0.0),
                    precipitation_chance=precipitation_chance_for_icon(icon),
                    condition=condition_for_icon(icon),
                    condition_code=wmo_code_for_icon(icon),
                    humidity=_to_int_or_none(day.get("humidity")),
                    wind_speed=_to_float_or_none(day.get("windSpeedDay")),
                    uv_index=_to_float_or_none(day.get("uvIndex")),
                )
            )
        return forecasts

    @staticmethod
    def _wearing_window(current: dict, hourly: dict) -> tuple[float, float] | None:
        current_time = current.get("time")
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        if not current_time or not times or not temps:
            return None
        try:
            now_local = datetime.fromisoformat(current_time)
        except ValueError:
            return None

        window: list[float] = []
        for slot_time, temp in zip(times, temps, strict=False):
            if temp is None:
                continue
            try:
                slot = datetime.fromisoformat(slot_time)
            except ValueError:
                continue
            if slot.date() == now_local.date() and slot.hour <= WEARING_DAY_END_HOUR:
                window.append(temp)

        if len(window) < MIN_WINDOW_HOURS:
            return None
        current_temp = current.get("temperature_2m")
        if current_temp is not None:
            window.append(current_temp)
        return min(window), max(window)

    async def get_daily_forecast(
        self, latitude: float, longitude: float, days: int = 7
    ) -> list[DailyForecast]:
        """
        Fetch daily forecast for a location.

        Args:
            latitude: Location latitude
            longitude: Location longitude
            days: Number of days to forecast (1-16)

        Returns:
            List of DailyForecast objects

        Raises:
            ValueError: If coordinates are out of bounds
            WeatherServiceError: If API request fails
        """
        self._validate_coordinates(latitude, longitude)

        if self.provider == "qweather":
            return await self._qweather_daily_forecast(latitude, longitude, days)

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "weather_code",
                "relative_humidity_2m_mean",
                "wind_speed_10m_max",
                "uv_index_max",
            ],
            "forecast_days": min(days, 16),
            "timezone": "auto",
        }

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                response = await client.get(f"{self.base_url}/forecast", params=params)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"Weather API error: {e}")
                raise WeatherServiceError(f"Failed to fetch forecast: {e}") from None

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temp_maxs = daily.get("temperature_2m_max", [])
        temp_mins = daily.get("temperature_2m_min", [])
        precip_probs = daily.get("precipitation_probability_max", [])
        weather_codes = daily.get("weather_code", [])
        humidities = daily.get("relative_humidity_2m_mean", [])
        wind_speeds = daily.get("wind_speed_10m_max", [])
        uv_indexes = daily.get("uv_index_max", [])

        forecasts = []
        for i, date in enumerate(dates):
            code = weather_codes[i] if i < len(weather_codes) else 0
            forecasts.append(
                DailyForecast(
                    date=date,
                    temp_max=temp_maxs[i] if i < len(temp_maxs) else 0,
                    temp_min=temp_mins[i] if i < len(temp_mins) else 0,
                    precipitation_chance=precip_probs[i] if i < len(precip_probs) else 0,
                    condition=self._interpret_weather_code(code),
                    condition_code=code,
                    humidity=humidities[i] if i < len(humidities) else None,
                    wind_speed=wind_speeds[i] if i < len(wind_speeds) else None,
                    uv_index=uv_indexes[i] if i < len(uv_indexes) else None,
                )
            )

        return forecasts

    async def get_tomorrow_weather(self, latitude: float, longitude: float) -> WeatherData:
        """
        Fetch tomorrow's weather forecast and return as WeatherData.

        This is used for day-before notifications where we need to recommend
        outfits based on tomorrow's expected weather.

        Args:
            latitude: Location latitude
            longitude: Location longitude

        Returns:
            WeatherData with tomorrow's forecast (avg temp, conditions)
        """
        forecasts = await self.get_daily_forecast(latitude, longitude, days=2)

        if len(forecasts) < 2:
            # Fallback to current weather if forecast fails
            logger.warning("Could not get tomorrow's forecast, using current weather")
            return await self.get_current_weather(latitude, longitude)

        tomorrow = forecasts[1]  # Index 0 is today, 1 is tomorrow

        # Use average of min/max for the representative temperature
        avg_temp = (tomorrow.temp_min + tomorrow.temp_max) / 2
        # Use the max temp for feels_like (daytime outfit)
        feels_like = tomorrow.temp_max

        return WeatherData(
            temperature=round(avg_temp, 1),
            feels_like=round(feels_like, 1),
            humidity=tomorrow.humidity if tomorrow.humidity is not None else 50,
            precipitation_chance=tomorrow.precipitation_chance,
            precipitation_mm=0,  # Open-Meteo's daily precipitation_sum is a different quantity than the mm-in-next-hour field this dataclass models
            wind_speed=tomorrow.wind_speed if tomorrow.wind_speed is not None else 0,
            condition=tomorrow.condition,
            condition_code=tomorrow.condition_code,
            is_day=True,  # Assume daytime for outfit recommendations
            uv_index=tomorrow.uv_index if tomorrow.uv_index is not None else 0,
            timestamp=datetime.utcnow(),
            temp_min=tomorrow.temp_min,
            temp_max=tomorrow.temp_max,
            window_min=tomorrow.temp_min,
            window_max=tomorrow.temp_max,
        )

    async def check_health(self) -> dict:
        """Check if the weather service is available."""
        if self.provider == "qweather":
            if self.qweather is None or not self.qweather.configured:
                return {"status": "unhealthy", "error": "QWeather API key is not configured"}
            try:
                # Any inhabited place works as a probe; Beijing always has a reading.
                await self.qweather.fetch_now(39.9042, 116.4074)
                return {"status": "healthy", "provider": "qweather"}
            except QWeatherError as e:
                return {"status": "unhealthy", "error": str(e)}

        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                # Simple request to check API availability
                response = await client.get(
                    f"{self.base_url}/forecast",
                    params={"latitude": 0, "longitude": 0, "current": "temperature_2m"},
                )
                if response.status_code == 200:
                    return {"status": "healthy", "provider": "open-meteo"}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

        return {"status": "unhealthy", "error": "Unknown error"}


class WeatherServiceError(Exception):
    pass


class GeocodingServiceError(WeatherServiceError):
    pass
