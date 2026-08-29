from app import create_app

app = create_app()

if __name__ == "__main__":
    try:
        from waitress import serve
    except ModuleNotFoundError:
        app.run(host="127.0.0.1", port=5000, debug=False)
    else:
        serve(app, host="127.0.0.1", port=5000, threads=4)
