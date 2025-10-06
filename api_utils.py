from flask import Flask, render_template_string
from flask_socketio import SocketIO
import threading
import asyncio
import aiohttp
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from sklearn.linear_model import LogisticRegression
import requests
import json
from pathlib import Path

# 캐시 파일 경로
CACHE_FILE = Path("cache.json")
# 메모리 캐시
cache = {}

# 계약 상세 정보 캐시 및 심볼 리스트
detail_map = {}
SYMBOLS = []

# Flask & SocketIO 설정
app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading')

# 클라이언트 HTML
HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>실시간 코인 분석</title>
  <script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.5.4/socket.io.min.js"></script>
</head>
<body>
  <h1>📊 실시간 MEXC 코인 추세 분석</h1>
  <ul id="results"></ul>
  <script>
    const socket = io();
    const ul = document.getElementById('results');

    socket.on('symbols', data => {
      ul.innerHTML = '';
      data.forEach(sym => {
        const li = document.createElement('li');
        li.textContent = `${sym}: ⚪ 분석중`;
        ul.appendChild(li);
      });
    });

    socket.on('update', data => {
      ul.innerHTML = '';
      data.results.forEach(([sym, res]) => {
        const li = document.createElement('li');
        li.textContent = `${sym}: ${res}`;
        ul.appendChild(li);
      });
    });
  </script>
</body>
</html>
"""

# 서버 시작 전 contract/detail 호출하여 detail_map, SYMBOLS 초기화
DETAIL_URL = "https://contract.mexc.com/api/v1/contract/detail"
try:
    resp = requests.get(DETAIL_URL, timeout=10)
    resp.raise_for_status()
    items = resp.json().get('data', [])
    for item in items:
        if item.get('quoteCoin') == 'USDT':
            symbol = item['symbol']
            detail_map[symbol] = item
    SYMBOLS = list(detail_map.keys())
except Exception as e:
    print(f"Detail fetch error: {e}")
    SYMBOLS = []

async def fetch_kline(session, symbol):
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval=1m&limit=100"
    try:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            rows = data.get('data', [])
            if not rows:
                return symbol, '⚪ 응답 없음'
            # timestamp, open, high, low, close, volume 순서 확인
            df = pd.DataFrame([{  
                'timestamp': int(k[0])//1000,
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            } for k in rows])
    except Exception:
        return symbol, '⚪ 요청 실패'

    if len(df) < 25:
        return symbol, '⚪ 데이터 부족'

    # 기술 지표
    df['rsi'] = RSIIndicator(df['close'], window=14).rsi()
    df['ma5'] = SMAIndicator(df['close'], window=5).sma_indicator()
    df['ma20'] = SMAIndicator(df['close'], window=20).sma_indicator()
    df['vol_chg'] = df['volume'].pct_change().fillna(0)
    df['prc_chg'] = df['close'].pct_change().fillna(0)
    df.dropna(inplace=True)
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    df.dropna(inplace=True)

    X = df[['rsi','ma5','ma20','vol_chg','prc_chg']]
    y = df['target']
    model = LogisticRegression().fit(X, y)
    latest = df.iloc[-1][['rsi','ma5','ma20','vol_chg','prc_chg']].values.reshape(1, -1)
    prob = model.predict_proba(latest)[0][1]

    # contract/detail의 riskLimitCustom[0]에서 mmr, imr 추출
    detail = detail_map.get(symbol, {})
    risk_list = detail.get('riskLimitCustom', [])
    if risk_list:
        mmr = risk_list[0].get('mmr', 0)
        imr = risk_list[0].get('imr', 0)
    else:
        mmr = detail.get('maintenanceMarginRate', 0)
        imr = detail.get('initialMarginRate', 0)
    # 리스크를 전체 가중치로 보고 조정
    total_risk = mmr + imr
    adjusted = prob * (1 - total_risk)

    if adjusted > 0.85:
        return symbol, f"🟢 Long ({adjusted*100:.1f}%)"
    elif adjusted < 0.15:
        return symbol, f"🔴 Short ({(1-adjusted)*100:.1f}%)"
    else:
        return symbol, f"⚪ 비추천 ({adjusted*100:.1f}%)"

async def rotating_analysis(group_size=30, interval=60):
    global cache
    async with aiohttp.ClientSession() as session:
        for sym in SYMBOLS:
            cache.setdefault(sym, '⚪ 분석중')
        while True:
            for i in range(0, len(SYMBOLS), group_size):
                batch = SYMBOLS[i:i+group_size]
                results = await asyncio.gather(*[fetch_kline(session, s) for s in batch])
                for s, r in results:
                    cache[s] = r
                CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                ordered = [(s, cache[s]) for s in SYMBOLS]
                socketio.emit('update', {'results': ordered})
                await asyncio.sleep(interval)

@app.route('/')
def index():
    return render_template_string(HTML)

@socketio.on('connect')
def on_connect():
    socketio.emit('symbols', SYMBOLS)
    initial = [(s, cache.get(s, '⚪ 분석중')) for s in SYMBOLS]
    socketio.emit('update', {'results': initial})

if __name__ == '__main__':
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
    threading.Thread(target=lambda: asyncio.run(rotating_analysis()), daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000)
