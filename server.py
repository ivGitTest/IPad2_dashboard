#!/usr/bin/env python3
"""Домашний дашборд для iPad 2: отдаёт одну страницу и кэшированную погоду.

Погода запрашивается сервером (а не iPad) из MET Norway (api.met.no, бэкенд
yr.no — бесплатно, без ключа, доступен из РФ) и кэшируется в памяти:
у iOS 9 проблемы с современными TLS/CORS, плюс так дашборд переживает
кратковременные сбои сети. (Open-Meteo не используется: заблокирован в РФ.)

Настройки — config.json рядом с server.py (поля см. в DEFAULT_CONFIG). Файл
читается один раз при старте процесса: изменения применяются перезапуском.
Отсутствующий/битый файл или отдельные невалидные значения не роняют сервер —
используются значения по умолчанию.
"""
import errno
import json
import math
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_CONFIG = {
    "server_ip": "0.0.0.0",  # на каком интерфейсе слушать; 0.0.0.0 — все
    "location": {"name": "Москва", "lat": 55.7558, "lon": 37.6173},
    "refresh_sec": 600,  # как часто опрашивать Open-Meteo, минимум 60
}

MIN_REFRESH_SEC = 60  # защита от банов Open-Meteo и бессмысленных значений
PORT = 80


def load_config():
    """config.json поверх дефолтов; невалидные поля заменяются дефолтом с warning."""
    cfg = {"server_ip": DEFAULT_CONFIG["server_ip"],
           "location": dict(DEFAULT_CONFIG["location"]),
           "refresh_sec": DEFAULT_CONFIG["refresh_sec"]}
    path = Path(__file__).with_name("config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        print(f"config.json: не прочитан ({exc}), значения по умолчанию", flush=True)
        return cfg
    if not isinstance(data, dict):
        print("config.json: ожидался JSON-объект, значения по умолчанию", flush=True)
        return cfg

    def warn(field, val):
        print(f"config.json: некорректное {field}={val!r}, значение по умолчанию",
              flush=True)

    val = data.get("server_ip", cfg["server_ip"])
    if isinstance(val, str) and val.strip():
        cfg["server_ip"] = val.strip()
    else:
        warn("server_ip", val)

    loc = data.get("location", {})
    if not isinstance(loc, dict):
        warn("location", loc)
        loc = {}
    val = loc.get("name", cfg["location"]["name"])
    if isinstance(val, str) and val.strip():
        cfg["location"]["name"] = val.strip()
    else:
        warn("location.name", val)
    for key in ("lat", "lon"):
        val = loc.get(key, cfg["location"][key])
        # bool — подкласс int в Python, координатой не считается
        if isinstance(val, (int, float)) and not isinstance(val, bool) \
                and (-90 <= val <= 90 if key == "lat" else -180 <= val <= 180):
            cfg["location"][key] = float(val)
        else:
            warn("location." + key, val)

    val = data.get("refresh_sec", cfg["refresh_sec"])
    if isinstance(val, (int, float)) and not isinstance(val, bool) \
            and val >= MIN_REFRESH_SEC:
        cfg["refresh_sec"] = int(val)
    else:
        warn("refresh_sec", val)
    return cfg


CONFIG = load_config()
# MET Norway требует идентифицирующий User-Agent, иначе банит.
USER_AGENT = "HomeDashboard/1.0 github.com/HomeDashboard"
MET_URL = (
    "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    f"?lat={CONFIG['location']['lat']}&lon={CONFIG['location']['lon']}"
)
MSK = timezone(timedelta(hours=3))  # Europe/Moscow: без DST с 2014, фиксировано +3

# Символ MET Norway (без суффикса _day/_night) -> (иконка, описание).
# Иконок ровно 4: sun/clouds/rain/snow.
SYMBOLS = {
    "clearsky": ("sun", "Ясно"),
    "fair": ("sun", "Преимущественно ясно"),
    "partlycloudy": ("clouds", "Переменная облачность"),
    "cloudy": ("clouds", "Пасмурно"),
    "fog": ("clouds", "Туман"),
    "lightrain": ("rain", "Небольшой дождь"),
    "rain": ("rain", "Дождь"),
    "heavyrain": ("rain", "Сильный дождь"),
    "lightrainshowers": ("rain", "Небольшой ливень"),
    "rainshowers": ("rain", "Ливень"),
    "heavyrainshowers": ("rain", "Сильный ливень"),
    "lightsleet": ("rain", "Мокрый снег"),
    "sleet": ("rain", "Мокрый снег"),
    "heavysleet": ("rain", "Мокрый снег"),
    "lightsleetshowers": ("rain", "Мокрый снег"),
    "sleetshowers": ("rain", "Мокрый снег"),
    "heavysleetshowers": ("rain", "Мокрый снег"),
    "lightsnow": ("snow", "Небольшой снег"),
    "snow": ("snow", "Снег"),
    "heavysnow": ("snow", "Снегопад"),
    "lightsnowshowers": ("snow", "Небольшой снег"),
    "snowshowers": ("snow", "Снег"),
    "heavysnowshowers": ("snow", "Снегопад"),
}


def split_symbol(code):
    """'clearsky_night' -> ('clearsky', 0), 'cloudy' -> ('cloudy', None)."""
    if code.endswith("_day"):
        return code[:-4], 1
    if code.endswith("_night"):
        return code[:-6], 0
    return code, None


def map_symbol(code):
    """Символ MET Norway -> (иконка, описание). Гроза: rain/snow по типу осадков."""
    base, _ = split_symbol(code)
    if base.endswith("andthunder"):
        return ("snow" if "snow" in base else "rain"), "Гроза"
    return SYMBOLS.get(base, ("clouds", "Облачно"))


def sun_is_up(dt_utc, lat, lon):
    """1 если над точкой lat/lon солнце выше горизонта (-50'), иначе 0.

    Приближение NOAA (точность ~2 мин — достаточно для темы день/ночь).
    Полярные день/ночь обрабатываются отдельно.
    """
    n = dt_utc.timetuple().tm_yday
    frac_hour = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    g = 2 * math.pi / 365 * (n - 1 + (frac_hour - lon / 15) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    if cos_ha > 1:
        return 0  # полярная ночь
    if cos_ha < -1:
        return 1  # полярный день
    ha_deg = math.degrees(math.acos(cos_ha))
    noon_utc_min = 720 - 4 * lon - eqtime  # солнечный полдень, минут от 00:00 UTC
    now_min = frac_hour * 60
    return 1 if noon_utc_min - ha_deg * 4 <= now_min <= noon_utc_min + ha_deg * 4 else 0


def symbol_is_day(code, dt_utc):
    """is_day из суффикса символа; нейтральные символы (cloudy, fog, ...) — по солнцу."""
    _, day = split_symbol(code)
    if day is not None:
        return day
    return sun_is_up(dt_utc, CONFIG["location"]["lat"], CONFIG["location"]["lon"])


def feels_like(temp_c, wind_ms):
    """«Ощущается как»: у MET Norway нет apparent_temperature — ветро-холодовой
    индекс (Environment Canada) для T<=10°C и ветра >4.8 км/ч, иначе = temp."""
    kmh = wind_ms * 3.6
    if temp_c <= 10 and kmh > 4.8:
        chill = (13.12 + 0.6215 * temp_c - 11.37 * kmh ** 0.16
                 + 0.3965 * temp_c * kmh ** 0.16)
        return round(min(chill, temp_c))  # не теплее фактической
    return round(temp_c)

_lock = threading.Lock()
_cache = {"ok": False}


def parse_dt(s):
    """'2026-11-07T11:00:00Z' -> aware datetime (UTC)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def symbol_of(entry):
    """Символ погоды записи timeseries: сводка ближайшего часа, иначе 6 часов."""
    data = entry.get("data", {})
    nxt = data.get("next_1_hours") or data.get("next_6_hours") or {}
    return nxt.get("summary", {}).get("symbol_code", "cloudy")


def parse_forecast(series, now_utc):
    """Прогноз на ближайшие 4 полных часа после текущего момента (записи
    timeseries у MET Norway почасовые и привязаны к полным часам)."""
    out = []
    for entry in series:
        dt = parse_dt(entry["time"])
        if dt <= now_utc:
            continue
        code = symbol_of(entry)
        icon, _ = map_symbol(code)
        out.append({
            "time": dt.astimezone(MSK).strftime("%H:%M"),
            "temp": round(float(entry["data"]["instant"]["details"]["air_temperature"])),
            "icon": icon,
            "is_day": symbol_is_day(code, dt),
        })
        if len(out) == 4:
            break
    return out


def fetch_weather():
    global _cache
    try:
        req = urllib.request.Request(MET_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        series = data["properties"]["timeseries"]
        cur = series[0]  # текущий час
        cur_dt = parse_dt(cur["time"])
        details = cur["data"]["instant"]["details"]
        temp = float(details["air_temperature"])
        wind = round(float(details["wind_speed"]), 1)
        code = symbol_of(cur)
        icon, desc = map_symbol(code)
        result = {
            "ok": True,
            "city": CONFIG["location"]["name"],
            "refresh": CONFIG["refresh_sec"],  # клиент считает по нему порог устаревания
            # Клиент считает по ним тему день/ночь (восход/закат, свои часы).
            "lat": CONFIG["location"]["lat"],
            "lon": CONFIG["location"]["lon"],
            "temp": round(temp),
            "feels": feels_like(temp, wind),
            "wind": wind,
            "icon": icon,
            "desc": desc,
            "is_day": symbol_is_day(code, cur_dt),
            "updated": cur_dt.astimezone(MSK).strftime("%H:%M"),
            "fetched": int(time.time()),  # epoch последнего удачного обновления
            "forecast": parse_forecast(series, datetime.now(timezone.utc)),
        }
    except Exception as exc:
        # city/refresh/lat/lon отдаём и при сбое: клиенту нужны город, порог
        # свежести и координаты для темы день/ночь.
        result = {"ok": False, "error": str(exc),
                  "city": CONFIG["location"]["name"],
                  "refresh": CONFIG["refresh_sec"],
                  "lat": CONFIG["location"]["lat"],
                  "lon": CONFIG["location"]["lon"]}
    with _lock:
        # При сбое сохраняем последние удачные данные.
        if result["ok"] or not _cache.get("ok"):
            _cache = result


def refresher():
    while True:
        fetch_weather()
        time.sleep(CONFIG["refresh_sec"])


INDEX = Path(__file__).with_name("index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX)
        elif self.path.startswith("/api/weather"):
            with _lock:
                body = json.dumps(_cache).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # web-app на iOS агрессивно кэширует
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    fetch_weather()
    threading.Thread(target=refresher, daemon=True).start()
    try:
        server = ThreadingHTTPServer((CONFIG["server_ip"], PORT), Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRNOTAVAIL:
            raise
        # Типичный случай — Docker: у контейнера нет IP хоста из конфига.
        # Слушаем все интерфейсы, дашборд всё равно доступен по IP хоста.
        print(f"server_ip={CONFIG['server_ip']} недоступен здесь ({exc}); "
              f"слушаю 0.0.0.0", flush=True)
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
