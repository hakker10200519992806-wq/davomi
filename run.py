#!/usr/bin/env python3
"""
LangLearn Studio - ishga tushirish skripti
Ishlatish: python run.py
"""
import os, sys, subprocess, time, webbrowser, threading

def check_deps():
    try:
        import flask, flask_sqlalchemy, flask_socketio
    except ImportError:
        print("📦 Kerakli kutubxonalar o'rnatilmoqda...")
        # Virtual environment yaratish (agar mavjud bo'lmasa)
        venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.venv')
        if not os.path.exists(venv_dir):
            print("🔧 Virtual environment yaratilmoqda...")
            subprocess.check_call([sys.executable, '-m', 'venv', venv_dir])
        # venv ichidagi pip bilan o'rnatish
        pip_path = os.path.join(venv_dir, 'bin', 'pip') if os.name != 'nt' else os.path.join(venv_dir, 'Scripts', 'pip.exe')
        if os.path.exists(pip_path):
            subprocess.check_call([pip_path, 'install',
                'flask', 'flask-sqlalchemy', 'flask-socketio', 'simple-websocket', '-q'])
            print(f"⚠️ Dasturni qayta ishga tushiring: {os.path.join(venv_dir, 'bin', 'python')} run.py")
            sys.exit(0)
        else:
            # Fallback: global pip
            subprocess.check_call([sys.executable, '-m', 'pip', 'install',
                'flask', 'flask-sqlalchemy', 'flask-socketio', 'simple-websocket', '-q'])

def open_browser():
    time.sleep(1.5)
    webbrowser.open('http://127.0.0.1:5000')

if __name__ == '__main__':
    check_deps()
    base = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base)
    for d in ['data', 'static/audio', 'static/img',
              'static/video', 'static/resources', 'templates']:
        os.makedirs(os.path.join(base, d), exist_ok=True)

    # index.html ni templates/ ga nusxalash
    src_html = os.path.join(base, 'index.html')
    dst_html = os.path.join(base, 'templates', 'index.html')
    if os.path.exists(src_html):
        import shutil
        shutil.copy2(src_html, dst_html)

    print("""
╔══════════════════════════════════════════╗
║        🌟 LangLearn Studio               ║
║   Til o'rgatish platformasi              ║
╠══════════════════════════════════════════╣
║  URL: http://127.0.0.1:5000              ║
║  To'xtatish: Ctrl+C                      ║
╚══════════════════════════════════════════╝
""")
    threading.Thread(target=open_browser, daemon=True).start()
    from app import app, socketio, init_db
    init_db()
    socketio.run(app, debug=False, port=5000, host='0.0.0.0')
