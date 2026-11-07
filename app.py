#!/usr/bin/env python3
"""EmailBoom 入口：python app.py"""
from emailboom import create_app
from config import Config

app = create_app()

if __name__ == "__main__":
    # 生产部署请使用 gunicorn/waitress 等 WSGI 服务器；
    # 开发环境直接运行本文件即可（后台调度器会自动拉起）。
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)
