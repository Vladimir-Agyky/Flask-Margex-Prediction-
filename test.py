import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from requests.exceptions import ReadTimeout, HTTPError
from urllib3.util.retry import Retry
import pandas as pd
from flask import Flask, render_template_string, jsonify

# === 세션 생성 및 재시도 설정 ===
def create_session(retries=3, backoff_factor=0.3, status_forcelist=(500,502,504)):
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# === 인터벌 매핑 ===
INTERVAL_MAP = {
    '1m':'Min1','5m':'Min5','15m':'Min15','30m':'Min30','60m':'Min60',
    '1h':'Min60','4h':'Hour4','8h':'Hour8','1d':'Day1'
}

# === 전역 상태 ===
results_cache = {}   # symbol -> {'signal':str, 'rate':float}
symbols = []
cycle_delay = 60.0   # 사이클 완료 후 대기 시간 (초)

# === 코인 리스트 가져오기 ===
def fetch_symbols():
    sess = create_session()
    url = 'https://contract.mexc.com/api/v1/contract/detail'
    resp = sess.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json().get('data', [])
    return [item['symbol'] for item in data if item['symbol'].endswith('_USDT')]

# === K라인 데이터 가져오기 ===
def get_klines(symbol, interval, limit=200):
    sess = create_session()
    api_i = INTERVAL_MAP.get(interval)
    url = f'https://contract.mexc.com/api/v1/contract/kline/{symbol}'
    resp = sess.get(url, params={'interval':api_i,'limit':limit}, timeout=10)
    resp.raise_for_status()
    payload = resp.json().get('data', [])
    df = pd.DataFrame(payload).iloc[:,:6]
    df.columns = ['openTime','open','high','low','close','volume']
    df['openTime'] = pd.to_datetime(df['openTime'], unit='ms')
    df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(float)
    return df

# === 기술 지표 계산 ===
def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100/(1+rs))

def compute_macd_diff(series):
    fast = series.ewm(span=12, adjust=False).mean()
    slow = series.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    sig = macd.ewm(span=9, adjust=False).mean()
    return macd - sig

# === 개별 심볼 분석 (연속 스코어링) ===
def analyze_symbol(symbol, interval='15m'):
    try:
        df = get_klines(symbol, interval)
        close = df['close']
        rsi = compute_rsi(close).iloc[-1]
        macd_diff = compute_macd_diff(close).iloc[-1]
        # 방향: MACD 양수→롱, 음수→숏
        signal = '롱' if macd_diff > 0 else '숏'
        # 성공율 대신 신호 강도: RSI 50 기준 거리 (0~100)
        strength = abs(rsi - 50) / 50 * 100
        rate = round(strength, 1)
        return symbol, signal, rate
    except Exception:
        # 오류 시 기본 롱 방향, rate 0
        return symbol, '롱', 0.0

# === 배치 업데이트 ===
from concurrent.futures import ThreadPoolExecutor, as_completed

def batch_update(interval='15m', workers=10):
    global results_cache
    while True:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(analyze_symbol, sym, interval) for sym in symbols]  # interval is '15m' for day trading timeframe
            for fut in as_completed(futures):
                sym, sig, rate = fut.result()
                results_cache[sym] = {'signal': sig, 'rate': rate}
        time.sleep(cycle_delay)

# === Flask 앱 및 라우트 ===
app = Flask(__name__)

HTML = '''
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MEXC 15M</title>
  <style>
    body { font-family: sans-serif; background: #f0f2f5; padding: 20px; }
    table { width: 90%; margin: 20px auto; border-collapse: collapse; }
    th, td { padding: 10px; border: 1px solid #ccc; text-align: center; }
    th { background: #4a90e2; color: #fff; cursor: pointer; }
    .롱 { background: #d4edda; color: #155724; }
    .숏 { background: #f8d7da; color: #721c24; }
    button { margin: 0 5px; padding: 5px 10px; }
  </style>
</head>
<body>
  <h1 style="text-align: center;">MEXC USDT Realtime Analysis(Made by Agyky)</h1>
  <div style="max-width: 900px; margin: auto; text-align: center;">
    <button onclick="setOrder(true)">Percentage ↑</button>
    <button onclick="setOrder(false)">Percentage ↓</button>
    <table id="sigTable">
      <thead>
        <tr><th>Item</th><th>Analysis</th><th>Percentage(%)</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
  <script>
    let asc = true;
    function setOrder(order) { asc = order; fetchData(); }
    function fetchData() {
      fetch('/api/results')
        .then(res => res.json())
        .then(data => {
          data.sort((a, b) => asc ? a.rate - b.rate : b.rate - a.rate);
          const tbody = document.querySelector('#sigTable tbody');
          tbody.innerHTML = '';
          data.forEach(item => {
            const tr = document.createElement('tr');
            tr.className = item.signal;
            tr.innerHTML = `<td>${item.symbol}</td><td>${item.signal}</td><td>${item.rate}%</td>`;
            tbody.appendChild(tr);
          });
        });
    }
    setInterval(fetchData, 10000);
    window.onload = fetchData;
  </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/results')
def api_results():
    arr = [{'symbol': k, 'signal': v['signal'], 'rate': v['rate']} for k, v in results_cache.items()]
    return jsonify(arr)

if __name__ == '__main__':
    symbols.extend(fetch_symbols())
    threading.Thread(target=batch_update, daemon=True).start()
    app.run(host='0.0.0.0', port=6915)
