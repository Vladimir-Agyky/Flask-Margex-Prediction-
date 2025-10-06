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

# --- 설정 ---
CACHE_FILE = Path("cache.json")
API_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"

# Flask & SocketIO 초기화 (threading 모드)
app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading')

# 전역 변수
SYMBOLS = []
cache = {}

# HTML 템플릿
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

    // 심볼 목록 초기 렌더링
    socket.on('symbols', data => {
      ul.innerHTML = '';
      data.forEach(sym => {
        const li = document.createElement('li');
        li.textContent = `${sym}: ⚪ 분석중`;
        ul.appendChild(li);
      });
    });

    // 결과 업데이트 렌더링
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

# 데이터 분석 함수
async def fetch_kline(session, symbol):
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval=1m&limit=100"
    try:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            if 'data' not in data:
                return symbol, '⚪ 응답 없음'
            rows = [{
                'timestamp': int(k[0])//1000,
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            } for k in data['data']]
            df = pd.DataFrame(rows)
    except:
        return symbol, '⚪ 요청 실패'

    try:
        if len(df) < 25:
            return symbol, '⚪ 데이터 부족'
        df['rsi'] = RSIIndicator(df['close'], window=14).rsi()
        df['ma5'] = SMAIndicator(df['close'], window=5).sma_indicator()
        df['ma20'] = SMAIndicator(df['close'], window=20).sma_indicator()
        df['volume_change'] = df['volume'].pct_change().fillna(0)
        df['price_change'] = df['close'].pct_change().fillna(0)
        df.dropna(inplace=True)
        df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
        df.dropna(inplace=True)

        X = df[['rsi','ma5','ma20','volume_change','price_change']]
        y = df['target']
        model = LogisticRegression().fit(X, y)
        latest = df.iloc[-1][['rsi','ma5','ma20','volume_change','price_change']].values.reshape(1, -1)
        prob = model.predict_proba(latest)[0][1]
        if prob > 0.85:
            return symbol, f"🟢 Long ({prob*100:.1f}%)"
        elif prob < 0.15:
            return symbol, f"🔴 Short ({(1-prob)*100:.1f}%)"
        else:
            return symbol, f"⚪ 비추천 ({prob*100:.1f}%)"
    except:
        return symbol, '⚪ 분석 실패'

# 백그라운드 분석 루프
async def rotating_analysis(group_size=30, interval=60):
    global cache
    async with aiohttp.ClientSession() as session:
        while True:
            for i in range(0, len(SYMBOLS), group_size):
                group = SYMBOLS[i:i+group_size]
                tasks = [fetch_kline(session, sym) for sym in group]
                results = await asyncio.gather(*tasks)

                # 캐시 및 파일 업데이트
                for sym, res in results:
                    cache[sym] = res
                with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cache, f, ensure_ascii=False)

                # 클라이언트로 전송
                ordered = [(sym, cache.get(sym, '⚪ 분석중')) for sym in SYMBOLS]
                socketio.emit('update', {'results': ordered})
                await asyncio.sleep(interval)

# 라우트
@app.route('/')
def index():
    return render_template_string(HTML)

# 소켓 이벤트
@socketio.on('connect')
def on_connect():
    # 심볼 목록
    socketio.emit('symbols', SYMBOLS)
    # 캐시 기반 초기 결과
    initial = [(sym, cache.get(sym, '⚪ 분석중')) for sym in SYMBOLS]
    socketio.emit('update', {'results': initial})

# 메인
if __name__ == '__main__':
    # 캐시 로드
    if CACHE_FILE.exists():
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    # 심볼 목록 로드
    resp = requests.get(API_DETAIL).json()
    SYMBOLS = [item['symbol'] for item in resp['data'] if item.get('quoteCoin') == 'USDT']

    # 백그라운드 분석 시작
    threading.Thread(target=lambda: asyncio.run(rotating_analysis()), daemon=True).start()

    # 서버 실행
    socketio.run(app, host='0.0.0.0', port=5000)
