#!/usr/bin/env python3
# dxy_btc_signals.py
import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_ID = "DTWEXBGS"  # Nominal Broad U.S. Dollar Index (proxy de fuerza del dolar, no es el DXY exacto)

RSI_OVERBOUGHT = 75
RSI_OVERSOLD = 25

STATE_FILE = "dxy_btc_alerted.json"


def get_usd_index_series(days_back=730, retries=3, retry_delay=10):
    """
    Pide el historico del indice de fuerza del dolar de la Fed (FRED,
    serie DTWEXBGS) de los ultimos 'days_back' dias naturales. Solo
    publica en dias habiles, y los valores faltantes vienen marcados
    como "." en vez de venir ausentes, hay que filtrarlos.
    Devuelve una lista de (fecha_str, valor) en orden cronologico.
    """
    api_key = os.environ.get("FRED_API_KEY")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = urllib.parse.urlencode({
        "series_id": FRED_SERIES_ID,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "sort_order": "asc",
    })
    url = f"{FRED_URL}?{params}"

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            result = []
            for obs in data["observations"]:
                if obs["value"] == ".":
                    continue  # dato faltante ese dia, se salta
                result.append((obs["date"], float(obs["value"])))
            return result
        except Exception as e:
            last_error = e
            print(f"Aviso: fallo al pedir indice del dolar de FRED (intento {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(retry_delay)

    raise last_error


def get_btc_series(interval="1day", outputsize=730, retries=3, retry_delay=10):
    """
    Pide el historico de BTC/USD a Twelve Data.
    Devuelve una lista de (fecha_str, close) en orden cronologico.
    """
    api_key = os.environ.get("TWELVEDATA_API_KEY")
    params = urllib.parse.urlencode({
        "symbol": "BTC/USD",
        "interval": interval,
        "outputsize": outputsize,
        "order": "ASC",
        "apikey": api_key,
    })
    url = f"{TWELVEDATA_URL}?{params}"

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            if "values" not in data:
                raise ValueError(f"respuesta sin 'values': {data}")
            result = []
            for v in data["values"]:
                date_str = v["datetime"][:10]
                result.append((date_str, float(v["close"])))
            return result
        except Exception as e:
            last_error = e
            print(f"Aviso: fallo al pedir BTC/USD (intento {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(retry_delay)

    raise last_error


def align_ratio(usd_index_series, btc_series):
    """
    Empareja ambas series por fecha (el indice del dolar solo tiene dato
    en dias habiles, BTC cotiza todos los dias, asi que solo se queda con
    las fechas presentes en ambas) y calcula el ratio indice/BTC para
    cada fecha comun, en orden cronologico.
    """
    btc_by_date = dict(btc_series)
    ratio = []
    for date_str, usd_value in usd_index_series:
        if date_str in btc_by_date:
            ratio.append((date_str, usd_value / btc_by_date[date_str]))
    return ratio


def weekly_from_daily(history):
    """Ultimo valor disponible de cada semana ISO."""
    groups = {}
    order = []
    for date_str, value in history:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        key = dt.isocalendar()[:2]
        if key not in groups:
            order.append(key)
        groups[key] = value
    return [groups[k] for k in order]


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
        return None, None
    if bearish:
        return "bajista", bearish_price
    if bullish:
        return "alcista", bullish_price
    return None, None


def zone_info(rsi):
    if rsi is None:
        return None
    if rsi >= RSI_OVERBOUGHT:
        return "sobrecompra"
    if rsi <= RSI_OVERSOLD:
        return "sobreventa"
    return None


def load_state(path=STATE_FILE):
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
    last_price = state["div_state"].get(key)
    if last_price is None:
        return True
    if last_price == 0:
        return price != 0
    return abs(price - last_price) / abs(last_price) > tolerance


def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN_MAG7")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
    urllib.request.urlopen(url, data=data, timeout=10)


def build_message(action_color, description):
    """
    Logica invertida orientada a BTC: si el ratio (indice del dolar / BTC)
    esta en sobrecompra o a punto de girar a la baja (divergencia
    bajista), es porque BTC lo esta haciendo mal frente al dolar ->
    señal alcista para BTC en cuanto revierta = POSIBLE COMPRA. Si esta
    en sobreventa o a punto de girar al alza, BTC ya lo ha hecho muy bien
    frente al dolar y esta maduro para corregir -> POSIBLE VENTA.
    """
    label = "POSIBLE SEÑAL DE COMPRA BTC" if action_color == "🟢" else "POSIBLE SEÑAL DE VENTA BTC"
    return f"{action_color} {label}\nDXY / BTCUSD\n{description}"


def process_signal(daily_values, weekly_values, state, sent):
    # --- RSI diario ---
    rsi_daily = compute_rsi(daily_values)
    label = zone_info(rsi_daily)
    if label:
        action_color = "🟢" if label == "sobrecompra" else "🔴"
        key = "dxybtc:rsi_daily"
        if key not in sent:
            desc = f"RSI diario en {label} ({rsi_daily:.0f})"
            msg = build_message(action_color, desc)
            print(msg)
            send_telegram(msg)
            sent.add(key)
        else:
            print(f"RSI diario en {label} pero ya avisado hoy")
    else:
        rd = f"{rsi_daily:.0f}" if rsi_daily is not None else "N/A"
        print(f"RSI diario: {rd} (sin señal)")

    # --- RSI semanal ---
    rsi_weekly = compute_rsi(weekly_values)
    label = zone_info(rsi_weekly)
    if label:
        action_color = "🟢" if label == "sobrecompra" else "🔴"
        key = "dxybtc:rsi_weekly"
        if key not in sent:
            desc = f"RSI semanal en {label} ({rsi_weekly:.0f})"
            msg = build_message(action_color, desc)
            print(msg)
            send_telegram(msg)
            sent.add(key)
        else:
            print(f"RSI semanal en {label} pero ya avisado hoy")
    else:
        rw = f"{rsi_weekly:.0f}" if rsi_weekly is not None else "N/A"
        print(f"RSI semanal: {rw} (sin señal)")

    # --- Divergencia diaria ---
    div_daily, div_daily_price = detect_divergence(daily_values, order=3, min_distance=5)
    key = "dxybtc:div_daily"
    if div_daily:
        if divergence_key_changed(state, key, div_daily_price):
            action_color = "🟢" if div_daily == "bajista" else "🔴"
            desc = f"Divergencia {div_daily} en diario"
            msg = build_message(action_color, desc)
            print(msg)
            send_telegram(msg)
            state["div_state"][key] = div_daily_price
        else:
            print(f"Divergencia {div_daily} diaria, mismo extremo ya avisado")
    elif key in state["div_state"]:
        del state["div_state"][key]

    # --- Divergencia semanal ---
    div_weekly, div_weekly_price = detect_divergence(weekly_values, order=2, min_distance=3)
    key = "dxybtc:div_weekly"
    if div_weekly:
        if divergence_key_changed(state, key, div_weekly_price):
            action_color = "🟢" if div_weekly == "bajista" else "🔴"
            desc = f"Divergencia {div_weekly} en semanal"
            msg = build_message(action_color, desc)
            print(msg)
            send_telegram(msg)
            state["div_state"][key] = div_weekly_price
        else:
            print(f"Divergencia {div_weekly} semanal, mismo extremo ya avisado")
    elif key in state["div_state"]:
        del state["div_state"][key]


def main():
    usd_index_daily = get_usd_index_series(days_back=730)
    btc_daily = get_btc_series(interval="1day", outputsize=730)

    daily_history = align_ratio(usd_index_daily, btc_daily)

    daily_values = [v for _, v in daily_history]
    weekly_values = weekly_from_daily(daily_history)

    if daily_history:
        print(f"Ratio Índice USD/BTC hoy ({daily_history[-1][0]}): {daily_history[-1][1]:.8f}")
    print(f"Histórico disponible: {len(daily_values)} días / {len(weekly_values)} semanas")

    state = load_state()
    sent = set(state.get("sent", []))

    process_signal(daily_values, weekly_values, state, sent)

    state["sent"] = sorted(sent)
    save_state(state)


if __name__ == "__main__":
    main()
