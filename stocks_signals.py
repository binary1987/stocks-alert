#!/usr/bin/env python3
# stocks_signals.py
import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "MSTR", "BMNR", "ORCL", "LLY", "COIN", "CRCL",
]

RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

STATE_FILE = "alerted_today.json"


def load_state(path=STATE_FILE):
    """
    Carga el estado de alertas. Tiene dos partes con vida distinta:
    - "sent": alertas de RSI en zona extrema, se resetea cada dia (UTC).
    - "div_state": ultimo precio del extremo que disparo cada divergencia,
      NO se resetea por fecha, solo cambia cuando aparece un pico/valle
      nuevo de verdad (asi no se repite el aviso mientras sea "la misma"
      divergencia, ni siquiera al dia siguiente).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    if data.get("date") != today:
        data["date"] = today
        data["sent"] = []

    data.setdefault("sent", [])
    data.setdefault("div_state", {})
    return data


def save_state(data, path=STATE_FILE):
    with open(path, "w") as f:
        json.dump(data, f)


def divergence_key_changed(state, key, price, tolerance=1e-6):
    """
    Compara el precio del extremo actual con el ultimo guardado para esa
    clave. Devuelve True si es una divergencia "nueva" (primera vez, o el
    extremo ha cambiado de verdad), False si sigue siendo la misma.
    """
    last_price = state["div_state"].get(key)
    if last_price is None:
        return True
    if last_price == 0:
        return price != 0
    return abs(price - last_price) / abs(last_price) > tolerance


def chunk_list(items, size):
    """Trocea una lista en sublistas de maximo 'size' elementos."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def get_batch(symbols, interval, outputsize):
    """
    Pide precios de varios simbolos a la vez (batch). Ojo: en Twelve Data
    cada simbolo consume 1 credito, un batch de 7 simbolos = 7 creditos,
    no 1. Devuelve un dict {symbol: [closes_ascendente]}.
    """
    api_key = os.environ.get("TWELVEDATA_API_KEY")
    params = urllib.parse.urlencode({
        "symbol": ",".join(symbols),
        "interval": interval,
        "outputsize": outputsize,
        "order": "ASC",
        "apikey": api_key,
    })
    url = f"{BASE_URL}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())

    result = {}
    for symbol in symbols:
        entry = data.get(symbol)
        if not entry or "values" not in entry:
            print(f"Aviso: sin datos para {symbol} ({entry})")
            continue
        closes = [float(v["close"]) for v in entry["values"]]
        result[symbol] = closes
    return result


def compute_rsi(closes, period=14):
    if len(closes) < 3:
        return None
    period = min(period, len(closes) - 1)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_rsi_series(closes, period=14):
    if len(closes) < period + 2:
        return []

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_value(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    rsis = [rsi_value(avg_gain, avg_loss)]
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsis.append(rsi_value(avg_gain, avg_loss))

    return rsis


def find_extrema(values, order, min_distance):
    peaks = []
    troughs = []
    n = len(values)
    i = order
    while i < n - order:
        window_before = values[max(0, i - order):i]
        window_after = values[i + 1:i + 1 + order]

        if window_before and window_after:
            is_peak = all(values[i] >= v for v in window_before) and all(values[i] >= v for v in window_after)
            is_trough = all(values[i] <= v for v in window_before) and all(values[i] <= v for v in window_after)

            if is_peak:
                peaks.append(i)
                i += min_distance
                continue
            if is_trough:
                troughs.append(i)
                i += min_distance
                continue
        i += 1

    return peaks, troughs


def detect_divergence(closes, order=3, min_distance=5, rsi_period=14):
    """Compara los 2 ultimos picos/valles de precio contra los de RSI para
    detectar divergencia. Devuelve (direccion, precio_del_extremo) o
    (None, None). El precio_del_extremo identifica el pico/valle concreto
    que genera la señal, para poder distinguir una divergencia "nueva" de
    una que sigue activa por el mismo punto de siempre."""
    rsi_series = compute_rsi_series(closes, period=rsi_period)
    if len(rsi_series) < order * 2 + min_distance + 2:
        return None, None

    aligned_closes = closes[-len(rsi_series):]
    peaks, troughs = find_extrema(aligned_closes, order, min_distance)

    bearish = False
    bearish_price = None
    if len(peaks) >= 2:
        i1, i2 = peaks[-2], peaks[-1]
        if aligned_closes[i2] > aligned_closes[i1] and rsi_series[i2] < rsi_series[i1]:
            bearish = True
            bearish_price = aligned_closes[i2]

    bullish = False
    bullish_price = None
    if len(troughs) >= 2:
        i1, i2 = troughs[-2], troughs[-1]
        if aligned_closes[i2] < aligned_closes[i1] and rsi_series[i2] > rsi_series[i1]:
            bullish = True
            bullish_price = aligned_closes[i2]

    if bearish and bullish:
        return None, None  # señales mixtas, no alertamos para evitar ruido
    if bearish:
        return "bajista", bearish_price
    if bullish:
        return "alcista", bullish_price
    return None, None


def zone_info(rsi):
    if rsi is None:
        return None, None
    if rsi >= RSI_OVERBOUGHT:
        return "sobrecompra", "🔴"
    if rsi <= RSI_OVERSOLD:
        return "sobreventa", "🟢"
    return None, None


def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN_MAG7")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
    urllib.request.urlopen(url, data=data, timeout=10)


MAX_SYMBOLS_PER_CALL = 8  # limite de creditos/minuto de Twelve Data (plan gratuito)


def fetch_all(symbols, interval, outputsize, call_counter, total_calls):
    """
    Pide los precios de 'symbols' troceando en grupos de MAX_SYMBOLS_PER_CALL,
    con una pausa de 65s entre cada llamada a la API (salvo la ultima de
    todas), para no superar el limite de creditos/minuto. Funciona igual de
    bien con 8 simbolos (1 sola llamada) que con 20 (varias llamadas).
    """
    merged = {}
    for chunk in chunk_list(symbols, MAX_SYMBOLS_PER_CALL):
        merged.update(get_batch(chunk, interval, outputsize))
        call_counter[0] += 1
        if call_counter[0] < total_calls:
            time.sleep(65)
    return merged


def main():
    state = load_state()
    sent = set(state.get("sent", []))

    chunks = chunk_list(SYMBOLS, MAX_SYMBOLS_PER_CALL)
    total_calls = len(chunks) * 2  # una vez para diario, otra para semanal
    call_counter = [0]

    daily_data = fetch_all(SYMBOLS, "1day", 365, call_counter, total_calls)
    weekly_data = fetch_all(SYMBOLS, "1week", 260, call_counter, total_calls)

    for symbol in SYMBOLS:
        daily_closes = daily_data.get(symbol)
        weekly_closes = weekly_data.get(symbol)

        if not daily_closes and not weekly_closes:
            print(f"{symbol}: sin datos disponibles")
            continue

        rsi_daily = compute_rsi(daily_closes) if daily_closes else None
        rsi_weekly = compute_rsi(weekly_closes) if weekly_closes else None

        label_daily, color_daily = zone_info(rsi_daily)
        label_weekly, color_weekly = zone_info(rsi_weekly)

        div_daily, div_daily_price = detect_divergence(daily_closes, order=3, min_distance=5) if daily_closes else (None, None)
        div_weekly, div_weekly_price = detect_divergence(weekly_closes, order=2, min_distance=3) if weekly_closes else (None, None)

        if label_daily is None and label_weekly is None and div_daily is None and div_weekly is None:
            rd = f"{rsi_daily:.0f}" if rsi_daily is not None else "N/A"
            rw = f"{rsi_weekly:.0f}" if rsi_weekly is not None else "N/A"
            print(f"{symbol}: sin señales (RSI diario {rd}, semanal {rw})")
            continue

        if label_daily:
            key = f"{symbol}:rsi_daily"
            if key not in sent:
                msg = f"🔔{color_daily} {symbol} — {label_daily}\nRSI diario: {rsi_daily:.0f}"
                print(msg)
                send_telegram(msg)
                sent.add(key)
            else:
                print(f"{symbol}: RSI diario en {label_daily} pero ya avisado hoy")

        if label_weekly:
            key = f"{symbol}:rsi_weekly"
            if key not in sent:
                msg = f"🔔{color_weekly} {symbol} — {label_weekly}\nRSI semanal: {rsi_weekly:.0f}"
                print(msg)
                send_telegram(msg)
                sent.add(key)
            else:
                print(f"{symbol}: RSI semanal en {label_weekly} pero ya avisado hoy")

        div_daily_key = f"{symbol}:div_daily"
        if div_daily:
            if divergence_key_changed(state, div_daily_key, div_daily_price):
                color = "🟢" if div_daily == "alcista" else "🔴"
                msg = f"🔔{color} {symbol} — divergencia {div_daily} (diario)"
                print(msg)
                send_telegram(msg)
                state["div_state"][div_daily_key] = div_daily_price
            else:
                print(f"{symbol}: divergencia {div_daily} diaria, mismo extremo ya avisado")
        elif div_daily_key in state["div_state"]:
            del state["div_state"][div_daily_key]

        div_weekly_key = f"{symbol}:div_weekly"
        if div_weekly:
            if divergence_key_changed(state, div_weekly_key, div_weekly_price):
                color = "🟢" if div_weekly == "alcista" else "🔴"
                msg = f"🔔{color} {symbol} — divergencia {div_weekly} (semanal)"
                print(msg)
                send_telegram(msg)
                state["div_state"][div_weekly_key] = div_weekly_price
            else:
                print(f"{symbol}: divergencia {div_weekly} semanal, mismo extremo ya avisado")
        elif div_weekly_key in state["div_state"]:
            del state["div_state"][div_weekly_key]

    state["sent"] = sorted(sent)
    save_state(state)


if __name__ == "__main__":
    main()
