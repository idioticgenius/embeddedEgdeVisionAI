# Flask server skeleton - single-camera POC (Cycle 1)
# Routes: /channels, /board_stats. SSE and login arrive in Cycle 2.
from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/channels')
def channels():
    return jsonify({'channels': [{'id': 0, 'state': 'idle'}]})

@app.route('/board_stats')
def board_stats():
    return jsonify({'fps': 0.0, 'rss_kb': 0})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
